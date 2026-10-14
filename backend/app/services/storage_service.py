"""
Storage layer (CLAUDE.md §10): Firestore-backed stores, chosen for access
control over the raw Excel file (real people's contact information). Every
store here has an in-memory twin with the same interface, used when
USE_MOCK_INTEGRATIONS=true so the pipeline can be exercised end to end
before any Firebase project exists. The Firestore implementations live in
firestore_stores.py and are imported only when actually needed.

Firestore bills per document read, so the interface is shaped around *which*
documents a caller needs rather than "give me everything": lookups by thread
or email, the leads whose follow-up is due, a count of today's sends.
Reading the whole list is reserved for the dashboard and explicit staff
actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.models.lead import Lead


class LeadStoreBase:
    def upsert_many(self, leads: list[Lead]) -> int:
        """Adds leads whose email isn't already stored (case-insensitive). Returns how many were added."""
        raise NotImplementedError

    def get(self, lead_id: str) -> Optional[Lead]:
        raise NotImplementedError

    def get_by_thread_id(self, thread_id: str) -> Optional[Lead]:
        raise NotImplementedError

    def find_by_email(self, email: str) -> Optional[Lead]:
        """The lead with this email address (case-insensitive), if any."""
        raise NotImplementedError

    def list_all(self) -> list[Lead]:
        raise NotImplementedError

    def list_all_light(self) -> list[Lead]:
        """
        Same rows as list_all(), minus draft_history and
        current_draft_body_html -- by far the two heaviest fields on a lead
        (full HTML email bodies, accumulating one per stage/edit). Only
        callers that never write these Lead objects back via save() may use
        this: the two excluded fields come back at their Pydantic defaults
        ([] / None), not their real stored values, so a save() of a lightly-
        fetched lead would silently wipe real history. See hydrate_lead()
        for the safe way to fill them back in right before a write.
        """
        raise NotImplementedError

    def list_due_for_follow_up(self, now: datetime) -> list[Lead]:
        """
        Full leads (complete, safe to save) that are waiting on a reply and
        whose follow-up wait has passed -- the only leads a tick must act on
        for timing. Everything still inside its wait window is never read.
        """
        raise NotImplementedError

    def list_contacted_light(self) -> list[Lead]:
        """Light leads that have been emailed at some point (sent, bounced, unsubscribed, exhausted)."""
        raise NotImplementedError

    def has_contacted(self) -> bool:
        raise NotImplementedError

    def count_sent_since(self, since: datetime) -> int:
        """How many leads' last email went out at or after `since` (naive UTC)."""
        raise NotImplementedError

    def reindex_follow_up_due(self) -> int:
        """
        Recomputes the stored follow-up due time for leads waiting on a reply.
        Call after the follow-up schedule changes, so already-waiting leads
        pick up the new wait. Returns how many were updated.
        """
        raise NotImplementedError

    def save(self, lead: Lead) -> None:
        raise NotImplementedError

    def email_exists(self, email: str) -> bool:
        return self.find_by_email(email) is not None


_CONTACTED_STATUSES = ("sent", "bounced", "unsubscribed", "exhausted")


class InMemoryLeadStore(LeadStoreBase):
    def __init__(self):
        self._leads: dict[str, Lead] = {}

    def upsert_many(self, leads: list[Lead]) -> int:
        added = 0
        for lead in leads:
            if self.email_exists(lead.email):
                continue
            self._leads[lead.id] = lead
            added += 1
        return added

    def get(self, lead_id: str) -> Optional[Lead]:
        return self._leads.get(lead_id)

    def get_by_thread_id(self, thread_id: str) -> Optional[Lead]:
        for lead in self._leads.values():
            if lead.thread_id == thread_id:
                return lead
        return None

    def find_by_email(self, email: str) -> Optional[Lead]:
        wanted = email.strip().lower()
        for lead in self._leads.values():
            if lead.email.lower() == wanted:
                return lead
        return None

    def list_all(self) -> list[Lead]:
        return list(self._leads.values())

    def list_all_light(self) -> list[Lead]:
        # Mirrors the Firestore-backed store's contract exactly (copies with
        # draft_history/current_draft_body_html reset to their defaults),
        # rather than just returning list_all() unchanged -- so tests
        # exercising this path actually catch a missing hydrate_lead() call
        # before a save, the same way the real store would.
        return [
            lead.model_copy(update={"draft_history": [], "current_draft_body_html": None})
            for lead in self._leads.values()
        ]

    def list_due_for_follow_up(self, now: datetime) -> list[Lead]:
        from app.services import schedule_service

        due = []
        for lead in self._leads.values():
            when = schedule_service.due_at(lead)
            if when is not None and when <= now:
                due.append(lead)
        return due

    def list_contacted_light(self) -> list[Lead]:
        return [l for l in self.list_all_light() if l.email_status.value in _CONTACTED_STATUSES]

    def has_contacted(self) -> bool:
        return any(l.email_status.value in _CONTACTED_STATUSES for l in self._leads.values())

    def count_sent_since(self, since: datetime) -> int:
        return sum(1 for l in self._leads.values() if l.last_sent_at and l.last_sent_at >= since)

    def reindex_follow_up_due(self) -> int:
        return 0  # due times are computed on the fly here; nothing is stored

    def save(self, lead: Lead) -> None:
        self._leads[lead.id] = lead


_HEAVY_FIELDS = {"draft_history", "current_draft_body_html"}
LIGHT_FIELDS = [f for f in Lead.model_fields if f not in _HEAVY_FIELDS]


def hydrate_lead(lead: Lead, store: LeadStoreBase) -> Lead:
    """
    Fills in the fields list_all_light() leaves out (draft_history,
    current_draft_body_html) from a fresh full fetch, merged onto the
    existing lead object in place rather than replacing it -- so any
    in-memory changes already applied this tick (e.g. tick.py repointing
    thread_id for a reply that landed on an unrecognized thread) survive.
    Must be called before any save() of a lead that came from
    list_all_light(): a save of an un-hydrated lead would overwrite the
    real draft_history in storage with the empty default.

    A lead that already carries either field is a full lead (a light one
    always has both at their empty defaults), so the extra read is skipped.
    """
    if lead.draft_history or lead.current_draft_body_html is not None:
        return lead
    full = store.get(lead.id)
    if full is not None:
        lead.draft_history = full.draft_history
        lead.current_draft_body_html = full.current_draft_body_html
    return lead


_DATETIME_FIELDS = (
    "created_time", "last_sent_at", "scheduled_call_time", "last_reply_at",
    "website_clicked_at", "get_started_clicked_at",
)


def _row_to_lead(row: dict) -> Lead:
    # Stored timestamps come back timezone-aware (Firestore Timestamps, or
    # ISO strings), but the rest of the pipeline compares against naive
    # datetime.utcnow() (see outreach_graph.py, send_batch.py) -- normalize to
    # naive UTC here, at the one place data crosses back in from storage,
    # rather than special-casing every comparison site.
    normalized = dict(row)
    for field in _DATETIME_FIELDS:
        value = normalized.get(field)
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            # Firestore hands back its own datetime subclass (DatetimeWithNanoseconds), which
            # breaks when written back (it loses an internal field when converted). Rebuild a
            # plain datetime first, then normalize to naive UTC.
            plain = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                             value.microsecond, tzinfo=value.tzinfo)
            if plain.tzinfo is not None:
                plain = plain.astimezone(timezone.utc).replace(tzinfo=None)
            normalized[field] = plain
    return Lead(**normalized)


_store: Optional[LeadStoreBase] = None


def get_lead_store() -> LeadStoreBase:
    global _store
    if _store is None:
        if settings.use_mock_integrations:
            _store = InMemoryLeadStore()
        else:
            from app.services.firestore_stores import FirestoreLeadStore

            _store = FirestoreLeadStore()
    return _store


class PipelineStateStore:
    """
    Tiny store for global pipeline state that isn't per-lead: the Gmail
    history.list cursor and the follow-up schedule. Deliberately NOT part of
    LeadStoreBase: these are single values for the whole mailbox/app.
    """

    def get_gmail_history_id(self) -> Optional[str]:
        raise NotImplementedError

    def set_gmail_history_id(self, history_id: str, expected_previous: Optional[str] = None) -> bool:
        """
        Writes the new cursor. If expected_previous is given, this is a
        compare-and-swap: the write only takes effect if the stored value
        still matches expected_previous, and returns False (skipping the
        write) otherwise -- meaning some other tick already advanced the
        cursor since we read it. Without this, the auto-tick and a manual
        "Check replies" click overlapping (or two auto-tick cycles
        overrunning their interval) could each read the same cursor, and
        whichever finishes last would silently clobber the other's advance
        with a stale value -- regressing the cursor and reprocessing
        whatever the first one already handled. Returns True if the write
        happened.
        """
        raise NotImplementedError

    def get_schedule(self) -> Optional[dict]:
        """The staff-edited follow-up schedule (see schedule_service.py), or None if never saved."""
        raise NotImplementedError

    def set_schedule(self, schedule: dict) -> None:
        raise NotImplementedError


class InMemoryPipelineStateStore(PipelineStateStore):
    def __init__(self):
        self._history_id: Optional[str] = None
        self._schedule: Optional[dict] = None

    def get_schedule(self) -> Optional[dict]:
        return self._schedule

    def set_schedule(self, schedule: dict) -> None:
        self._schedule = schedule

    def get_gmail_history_id(self) -> Optional[str]:
        return self._history_id

    def set_gmail_history_id(self, history_id: str, expected_previous: Optional[str] = None) -> bool:
        if expected_previous is not None and self._history_id != expected_previous:
            return False
        self._history_id = history_id
        return True


_state_store: Optional[PipelineStateStore] = None


def get_pipeline_state_store() -> PipelineStateStore:
    global _state_store
    if _state_store is None:
        if settings.use_mock_integrations:
            _state_store = InMemoryPipelineStateStore()
        else:
            from app.services.firestore_stores import FirestorePipelineStateStore

            _state_store = FirestorePipelineStateStore()
    return _state_store


class CompanyProfileStore:
    """
    One researched profile per company (app/services/company_research.py),
    keyed by normalized company name -- shared by every lead at that company,
    which is what makes the research a one-time cost per company.
    """

    def get(self, key: str):
        raise NotImplementedError

    def save(self, profile) -> None:
        raise NotImplementedError


class InMemoryCompanyProfileStore(CompanyProfileStore):
    def __init__(self):
        self._profiles: dict = {}

    def get(self, key: str):
        return self._profiles.get(key)

    def save(self, profile) -> None:
        self._profiles[profile.key] = profile


_company_profile_store: Optional[CompanyProfileStore] = None


def get_company_profile_store() -> CompanyProfileStore:
    global _company_profile_store
    if _company_profile_store is None:
        if settings.use_mock_integrations:
            _company_profile_store = InMemoryCompanyProfileStore()
        else:
            from app.services.firestore_stores import FirestoreCompanyProfileStore

            _company_profile_store = FirestoreCompanyProfileStore()
    return _company_profile_store


class DraftChangeConflict(Exception):
    """The draft-change state was changed by someone else since the caller read it."""


class DraftChangeStore:
    """
    The single versioned document behind the Modify draft page. put_state is a
    compare-and-swap: it only succeeds if the stored version is still the one
    the caller started from, so two staff members editing at once can't
    silently overwrite each other (the second gets a conflict and reloads).
    """

    def get_state(self):
        raise NotImplementedError

    def put_state(self, state, expected_version: int):
        raise NotImplementedError


class InMemoryDraftChangeStore(DraftChangeStore):
    def __init__(self):
        from app.models.draft_changes import DraftChangeState

        self._state = DraftChangeState()

    def get_state(self):
        return self._state.model_copy(deep=True)

    def put_state(self, state, expected_version: int):
        if self._state.version != expected_version:
            raise DraftChangeConflict()
        self._state = state.model_copy(update={"version": expected_version + 1}, deep=True)
        return self._state.model_copy(deep=True)


_draft_change_store: Optional[DraftChangeStore] = None


def get_draft_change_store() -> DraftChangeStore:
    global _draft_change_store
    if _draft_change_store is None:
        if settings.use_mock_integrations:
            _draft_change_store = InMemoryDraftChangeStore()
        else:
            from app.services.firestore_stores import FirestoreDraftChangeStore

            _draft_change_store = FirestoreDraftChangeStore()
    return _draft_change_store
