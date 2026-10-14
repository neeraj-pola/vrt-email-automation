"""
Follow-up schedule: how many days to wait before the next follow-up (§8).

The default is 3 days (settings.follow_up_wait_days), but staff can override
it for a specific company and/or for a specific follow-up stage. The most
specific rule wins:

    1. a company override        (e.g. "Acme Corp" -> 5 days, every stage)
    2. a follow-up stage override (e.g. the wait before follow-up 2 -> 7 days)
    3. the default

"Stage" here means which follow-up is next: 1 = the wait after the initial
email, 2 = the wait after follow-up 1, 3 = the wait after follow-up 2.

Persisted through PipelineStateStore so it survives restarts, and cached in
memory so the tick (which checks every lead) never hits the database for it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.models.lead import AIResponseClassification, EmailStatus, Lead

MAX_WAIT_DAYS = 90


class Schedule(BaseModel):
    default_days: int = Field(default_factory=lambda: settings.follow_up_wait_days, ge=1, le=MAX_WAIT_DAYS)
    by_company: dict[str, int] = Field(default_factory=dict)
    by_stage: dict[str, int] = Field(default_factory=dict)  # keys "1".."N"

    @field_validator("by_company", "by_stage")
    @classmethod
    def _days_in_range(cls, v: dict[str, int]) -> dict[str, int]:
        for key, days in v.items():
            if not 1 <= days <= MAX_WAIT_DAYS:
                raise ValueError(f"{key!r}: wait must be between 1 and {MAX_WAIT_DAYS} days")
        return v

    def wait_days_for(self, lead: Lead) -> int:
        company = _norm(lead.company)
        if company:
            for name, days in self.by_company.items():
                if _norm(name) == company:
                    return days
        stage = str(lead.follow_up_count + 1)
        if stage in self.by_stage:
            return self.by_stage[stage]
        return self.default_days


def _norm(name: Optional[str]) -> str:
    return (name or "").strip().lower()


_lock = threading.Lock()
_cached: Optional[Schedule] = None


def get_schedule() -> Schedule:
    global _cached
    with _lock:
        if _cached is None:
            from app.services.storage_service import get_pipeline_state_store

            raw = get_pipeline_state_store().get_schedule()
            _cached = Schedule(**raw) if raw else Schedule()
        return _cached


def set_schedule(schedule: Schedule) -> Schedule:
    global _cached
    from app.services.storage_service import get_lead_store, get_pipeline_state_store

    get_pipeline_state_store().set_schedule(schedule.model_dump())
    with _lock:
        _cached = schedule
    # Leads already waiting on a reply carry a stored due time; bring it in line with the new wait.
    get_lead_store().reindex_follow_up_due()
    return schedule


def reset_cache() -> None:
    """For tests."""
    global _cached
    with _lock:
        _cached = None


def wait_days_for(lead: Lead) -> int:
    return get_schedule().wait_days_for(lead)


_TERMINAL = (AIResponseClassification.interested, AIResponseClassification.not_interested)


def due_at(lead: Lead) -> Optional[datetime]:
    """
    When a lead waiting on a reply is next due for the follow-up loop to act
    (send the next follow-up, or give up after the last one), as naive UTC --
    or None if it isn't waiting on one (never sent, replied with a terminal
    outcome, stopped). This is the one definition of "due": the in-memory
    store computes it on the fly and the Firestore store saves it on the lead
    so a tick can ask for just the due leads instead of reading every lead.
    """
    if lead.email_status != EmailStatus.sent or lead.last_sent_at is None:
        return None
    if lead.ai_response_classification in _TERMINAL:
        return None
    return lead.last_sent_at + timedelta(days=wait_days_for(lead))
