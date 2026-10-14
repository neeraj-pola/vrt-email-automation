"""
Staff changes to the first-email prompt (see app/models/draft_changes.py).

Reads and saves the versioned state, picks which enabled changes cover a
lead, and renders them as the block that goes into the prompt. Cached in
memory (invalidated on every save) so drafting a batch of leads doesn't read
the store once per lead.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.knowledge.draft_flow import ALL_STAGES, stage_title
from app.models.draft_changes import DraftChange, DraftChangeState, DraftChangeStateUpdate
from app.models.lead import Lead
from app.services.storage_service import DraftChangeConflict, get_draft_change_store

_lock = threading.Lock()
_cached: Optional[DraftChangeState] = None


def _norm(name: Optional[str]) -> str:
    return " ".join((name or "").split()).lower()


def get_state() -> DraftChangeState:
    global _cached
    with _lock:
        if _cached is None:
            _cached = get_draft_change_store().get_state()
        return _cached.model_copy(deep=True)


def save_state(update: DraftChangeStateUpdate) -> DraftChangeState:
    """Replaces the whole state. Raises DraftChangeConflict if someone changed it first."""
    global _cached
    new_state = DraftChangeState(version=update.expected_version, changes=update.changes, saved=update.saved)
    saved = get_draft_change_store().put_state(new_state, update.expected_version)
    with _lock:
        _cached = saved
    return saved.model_copy(deep=True)


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None


def changes_for(lead: Lead, state: Optional[DraftChangeState] = None) -> list[DraftChange]:
    """Enabled changes that cover this lead's company, in the order staff added them."""
    state = state or get_state()
    company = _norm(lead.company)
    out = []
    for change in state.changes:
        if not change.enabled:
            continue
        if change.companies is not None and company not in {_norm(c) for c in change.companies}:
            continue
        out.append(change)
    return out


def prompt_block(changes: list[DraftChange]) -> str:
    """The text that goes into the first-email prompt ('' when there is nothing to add)."""
    if not changes:
        return ""
    lines = []
    for c in changes:
        where = "Entire email" if c.stages == [ALL_STAGES] else " + ".join(stage_title(s) for s in c.stages)
        lines.append(f"- [{where}] {c.text}")
    return (
        "Instructions from our team for this email. Follow each one exactly: if one says to offer evening "
        "times, the email must say so. Each names the part of the email it is about. Where one conflicts with "
        "a line under Defaults below (length, subject style), the instruction wins. They never override the "
        "Hard rules below: if one asks for an invented fact, a price, a statistic or a promised result, "
        "leave that part out.\n" + "\n".join(lines)
    )


def describe_applied(changes: list[DraftChange]) -> str:
    """Short phrase for the draft's personalization note, e.g. 'Short intro, Evening times'."""
    labels = [(c.name or (c.text[:40] + ("…" if len(c.text) > 40 else ""))) for c in changes]
    return ", ".join(labels)
