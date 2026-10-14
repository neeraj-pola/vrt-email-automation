"""
Staff changes to how the first email is written (the Modify draft page).

A DraftChange is one instruction in plain words, aimed at the whole email or
at specific stages of the flow (app/knowledge/draft_flow.py), and at every
company or just some. Enabled changes are put into the first-email prompt
for the leads they cover.

A SavedChange is a named change kept in a library so it can be reused on
other companies later with one click.

The whole thing is one versioned document (DraftChangeState): the page edits
it as a whole, which is what makes Undo and "Back to initial state" exact,
and the version number stops two staff members silently overwriting each
other.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from app.knowledge.draft_flow import ALL_STAGES, EDITABLE_STAGE_IDS

MAX_TEXT = 500
MAX_NAME = 60
MAX_CHANGES = 100
MAX_SAVED = 100


def _clean_stages(stages: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(s.strip() for s in stages if s and s.strip()))
    if not cleaned:
        raise ValueError("pick at least one stage")
    bad = [s for s in cleaned if s != ALL_STAGES and s not in EDITABLE_STAGE_IDS]
    if bad:
        raise ValueError(f"unknown or locked stage(s): {bad}")
    return [ALL_STAGES] if ALL_STAGES in cleaned else cleaned  # "entire email" subsumes the rest


def _clean_text(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise ValueError("the instruction is empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"keep an instruction under {MAX_TEXT} characters")
    return text


def _clean_name(name: Optional[str]) -> Optional[str]:
    name = " ".join((name or "").split())
    if len(name) > MAX_NAME:
        raise ValueError(f"keep a name under {MAX_NAME} characters")
    return name or None


class DraftChange(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    stages: list[str]
    companies: Optional[list[str]] = None  # None = every company
    name: Optional[str] = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _text = field_validator("text")(_clean_text)
    _stages = field_validator("stages")(_clean_stages)
    _name = field_validator("name")(_clean_name)

    @field_validator("companies")
    @classmethod
    def _companies(cls, v):
        if v is None:
            return None
        cleaned = list(dict.fromkeys(" ".join(c.split()) for c in v if c and c.strip()))
        if not cleaned:
            raise ValueError("pick at least one company, or apply to all companies")
        if len(cleaned) > 500:
            raise ValueError("too many companies in one change")
        return cleaned


class SavedChange(BaseModel):
    name: str
    text: str
    stages: list[str]

    _text = field_validator("text")(_clean_text)
    _stages = field_validator("stages")(_clean_stages)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        cleaned = _clean_name(v)
        if not cleaned:
            raise ValueError("a saved change needs a name")
        return cleaned


class DraftChangeState(BaseModel):
    version: int = 0
    changes: list[DraftChange] = Field(default_factory=list)
    saved: list[SavedChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _limits(self):
        if len(self.changes) > MAX_CHANGES:
            raise ValueError(f"at most {MAX_CHANGES} changes")
        if len(self.saved) > MAX_SAVED:
            raise ValueError(f"at most {MAX_SAVED} saved changes")
        names = [s.name.lower() for s in self.saved]
        if len(names) != len(set(names)):
            raise ValueError("saved change names must be unique")
        ids = [c.id for c in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate change ids")
        return self


class DraftChangeStateUpdate(BaseModel):
    """What the page sends: the whole new content plus the version it started from."""
    expected_version: int
    changes: list[DraftChange]
    saved: list[SavedChange]
