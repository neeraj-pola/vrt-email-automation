"""Draft generation, manual edit, and chatbot-edit endpoints (§6.2, §6.3, §9)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_store, require_staff
from app.api.schemas import (
    AdvisorAskRequest,
    AdvisorAskResponse,
    ChatbotEditRequest,
    LeadIdsRequest,
    ManualDraftEditRequest,
    PreviewDraftRequest,
    RestoreVersionRequest,
)
from app.models.lead import Lead
from app.pipeline.draft_generation import (
    apply_chatbot_edit,
    apply_manual_edit,
    generate_drafts_for_leads,
    restore_draft_version,
)
from app.services import llm_service
from app.services.email_template import render_email_html
from app.services.storage_service import LeadStoreBase

router = APIRouter(prefix="/drafts", tags=["drafts"])


@router.post("/generate", response_model=list[Lead])
async def generate_drafts(body: LeadIdsRequest, store: LeadStoreBase = Depends(get_store),
                           _staff=Depends(require_staff)):
    leads = [store.get(lid) for lid in body.lead_ids]
    missing = [lid for lid, l in zip(body.lead_ids, leads) if l is None]
    if missing:
        raise HTTPException(status_code=404, detail=f"Leads not found: {missing}")
    # Research + an LLM call per lead: blocking network I/O, so off the event loop
    # (same reason as /pipeline/send) or one batch would freeze every other request.
    updated, errors = await asyncio.to_thread(generate_drafts_for_leads, leads, store)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return updated


@router.patch("/{lead_id}", response_model=Lead)
async def edit_draft_manually(lead_id: str, body: ManualDraftEditRequest,
                               store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        return apply_manual_edit(lead, body.subject, body.body_html, store)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/{lead_id}/chatbot", response_model=Lead)
async def edit_draft_via_chatbot(lead_id: str, body: ChatbotEditRequest,
                                  store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """GPT-4o-powered rewrite/structural change, staff-directed (§6, §9)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        return apply_chatbot_edit(lead, body.instruction, store)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/{lead_id}/restore-version", response_model=Lead)
async def restore_version(lead_id: str, body: RestoreVersionRequest,
                           store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """Staff picks an earlier version from the draft-history dropdown (§9)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        return restore_draft_version(lead, body.version_index, store)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/{lead_id}/ask", response_model=AdvisorAskResponse)
async def ask_about_lead(lead_id: str, body: AdvisorAskRequest,
                          store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Advisor chat: talking-points / "what should we say" help for a lead,
    available at ANY point (before or after the initial email is sent) --
    unlike /chatbot, this never edits current_draft_* or requires an
    unlocked draft, since it's not writing to the lead at all.
    """
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    history = [turn.model_dump() for turn in body.history]
    answer = llm_service.answer_lead_question(lead, body.question, history)
    return AdvisorAskResponse(answer=answer)


@router.post("/{lead_id}/preview")
async def preview_draft(lead_id: str, body: PreviewDraftRequest = PreviewDraftRequest(),
                         store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Full rendered HTML (branded shell + portfolio showcase + footer) for the
    dashboard's draft preview, so staff see exactly what the lead will
    receive -- not just the raw inner content that's stored/edited.

    Defaults to the current draft, but accepts an explicit subject/body_html
    override so the SAME rendering (not a second, hand-rolled one in the
    frontend) can preview a superseded version from draft_history too --
    staff should be able to see how an old version actually looked, not
    just its raw HTML source.

    The Unsubscribe link is deliberately inert here
    (interactive_unsubscribe=False): this is a preview a staff member is
    just looking at, not an email in an inbox, and it must not be possible
    to actually unsubscribe the lead by clicking around a draft that hasn't
    even been sent.
    """
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    subject = body.subject if body.subject is not None else lead.current_draft_subject
    body_html = body.body_html if body.body_html is not None else lead.current_draft_body_html
    if not subject or not body_html:
        raise HTTPException(status_code=404, detail="No draft to preview yet")
    html = render_email_html(lead, subject, body_html, interactive_unsubscribe=False)
    return {"subject": subject, "html": html}
