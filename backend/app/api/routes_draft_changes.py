"""
Modify draft page: the flow of the first email, and staff changes to it.

`GET /draft-changes/flow`        -- the stages of the flowchart (app/knowledge/draft_flow.py)
`GET /draft-changes`             -- the current changes + saved (named) changes + version
`PUT /draft-changes`             -- replace the whole state (compare-and-swap on `expected_version`)
`POST /draft-changes/preview`    -- a real before/after draft for one lead, nothing saved
`POST /draft-changes/regenerate` -- rewrite unsent drafts so they pick up the active changes
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_store, require_staff
from app.config import settings
from app.knowledge.draft_flow import ALL_STAGES, FLOW_STAGES
from app.models.draft_changes import DraftChange, DraftChangeState, DraftChangeStateUpdate
from app.models.lead import EmailStatus
from app.pipeline.draft_generation import generate_drafts_for_leads
from app.services import company_research, draft_changes_service, llm_service
from app.services.email_template import render_email_html
from app.services.storage_service import DraftChangeConflict, LeadStoreBase

router = APIRouter(prefix="/draft-changes", tags=["draft-changes"])


class FlowStageOut(BaseModel):
    id: str
    no: int
    title: str
    description: str
    chips: list[str]
    builtin_rule: Optional[str]
    editable: bool


class ProposedChange(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    stages: list[str]


class PreviewRequest(BaseModel):
    lead_id: str
    proposed: Optional[ProposedChange] = None


class RenderedDraft(BaseModel):
    subject: str
    html: str


class PreviewResponse(BaseModel):
    before: RenderedDraft
    after: RenderedDraft
    simulated: bool = False  # True in mock mode: the fixed template, no model call


class RegenerateRequest(BaseModel):
    companies: Optional[list[str]] = None  # None = every company


class RegenerateResponse(BaseModel):
    regenerated: int
    errors: list[dict]


@router.get("/flow", response_model=list[FlowStageOut])
async def get_flow(_staff=Depends(require_staff)):
    return [FlowStageOut(id=s.id, no=s.no, title=s.title, description=s.description, chips=list(s.chips),
                         builtin_rule=s.builtin_rule, editable=s.editable) for s in FLOW_STAGES]


@router.get("", response_model=DraftChangeState)
async def get_changes(_staff=Depends(require_staff)):
    return draft_changes_service.get_state()


@router.put("", response_model=DraftChangeState)
async def put_changes(body: DraftChangeStateUpdate, _staff=Depends(require_staff)):
    try:
        return draft_changes_service.save_state(body)
    except DraftChangeConflict as e:
        raise HTTPException(
            status_code=409,
            detail="Someone else changed these instructions since you opened the page. Reload to see their changes.",
        ) from e


def _render(lead, subject: str, body_html: str) -> RenderedDraft:
    # Preview, not a send: the unsubscribe link renders inert (same as the draft preview).
    return RenderedDraft(subject=subject, html=render_email_html(lead, subject, body_html, interactive_unsubscribe=False))


def _build_preview(lead, proposed: Optional[ProposedChange]) -> PreviewResponse:
    if settings.use_mock_integrations:
        draft = llm_service.generate_initial_draft(lead)
        rendered = _render(lead, draft.subject, draft.body_html)
        return PreviewResponse(before=rendered, after=rendered, simulated=True)

    profile = company_research.get_cached(lead)  # never researches: a preview must stay cheap
    active = draft_changes_service.changes_for(lead)
    extra = []
    if proposed is not None:
        extra = [DraftChange(text=proposed.text, stages=proposed.stages)]
    # Low temperature so "before" and "after" differ because of the change, not because of sampling noise.
    before = llm_service.generate_personalized_initial_draft(lead, profile, active, temperature=0.2)
    after = llm_service.generate_personalized_initial_draft(lead, profile, active + extra, temperature=0.2)
    return PreviewResponse(before=_render(lead, before.subject, before.body_html),
                           after=_render(lead, after.subject, after.body_html))


@router.post("/preview", response_model=PreviewResponse)
async def preview(body: PreviewRequest, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    lead = store.get(body.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if body.proposed is not None:
        try:
            DraftChange(text=body.proposed.text, stages=body.proposed.stages)  # same validation as saving
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    try:
        return await asyncio.to_thread(_build_preview, lead, body.proposed)
    except Exception as e:  # noqa: BLE001 -- model/network failure: say so instead of a bare 500
        raise HTTPException(status_code=502, detail=f"Couldn't generate a preview: {e}") from e


def _regenerate(store: LeadStoreBase, companies: Optional[list[str]]) -> RegenerateResponse:
    scope = None if companies is None else {" ".join(c.split()).lower() for c in companies}
    targets = []
    for lead in store.list_all():
        unsent = lead.email_status == EmailStatus.not_sent and not lead.draft_locked
        in_scope = scope is None or " ".join((lead.company or "").split()).lower() in scope
        if unsent and in_scope:
            targets.append(lead)
    updated, errors = generate_drafts_for_leads(targets, store)  # a few in parallel, see draft_generation
    return RegenerateResponse(regenerated=len(updated), errors=errors)


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(body: RegenerateRequest, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """Rewrites only drafts that were never sent. Sent emails and locked drafts are never touched."""
    return await asyncio.to_thread(_regenerate, store, body.companies)
