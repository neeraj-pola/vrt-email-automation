"""
Stage 2-3 (§6.2, §6.3): generate initial drafts for review, and apply staff
edits (direct or chatbot-driven) before a draft is sent.

Every path that overwrites lead.current_draft_* first snapshots the
outgoing version into lead.draft_history, so staff can switch back to an
earlier version instead of it just being gone (previously a chatbot rewrite
replaced the draft with no way back). See also outreach_graph.py, which
does the same for the automatic pipeline's own stage transitions (initial ->
follow-up 1 -> follow-up 2, ...).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.models.lead import DraftVersion, DraftVersionSource, Lead
from app.services import company_research, draft_changes_service, llm_service
from app.services.storage_service import LeadStoreBase

logger = logging.getLogger(__name__)

# Company research and the personalized draft are network + LLM calls; a few
# at a time keeps a 30-lead batch to minutes instead of a long sequential
# crawl, without hammering a search API or a single company's website.
_MAX_PARALLEL_DRAFTS = 4


def _snapshot_current_draft(lead: Lead, source: DraftVersionSource, stage: str) -> None:
    if lead.current_draft_subject is None and lead.current_draft_body_html is None:
        return  # nothing to preserve yet (first-ever generation)
    lead.draft_history.append(DraftVersion(
        subject=lead.current_draft_subject or "",
        body_html=lead.current_draft_body_html or "",
        source=source,
        stage=stage,
    ))


def _initial_draft(lead: Lead, force_research: bool = False) -> tuple[llm_service.DraftOutput, str | None]:
    """
    The personalized first email, falling back to the fixed template if
    anything about personalizing fails (no OpenAI key, a bad generation, a
    network error) -- a lead must never be left without a draft because the
    nice-to-have step broke. Mock-integration mode skips straight to the
    template: no network calls in local/synthetic testing.
    """
    if settings.use_mock_integrations:
        return llm_service.generate_initial_draft(lead), None
    try:
        profile = company_research.get_or_research(lead, force=force_research)
    except Exception as e:  # noqa: BLE001 -- research is optional, never blocks a draft
        logger.warning("Company research failed for lead %s: %s", lead.id, e)
        profile = None
    try:
        # Staff instructions from the Modify draft page that cover this lead's company.
        changes = draft_changes_service.changes_for(lead)
        personalized = llm_service.generate_personalized_initial_draft(lead, profile, changes)
        return llm_service.DraftOutput(subject=personalized.subject, body_html=personalized.body_html), personalized.note
    except Exception as e:  # noqa: BLE001
        logger.warning("Personalized draft failed for lead %s, using the template: %s", lead.id, e)
        return llm_service.generate_initial_draft(lead), "Standard template (personalization was unavailable)."


def generate_draft_for_lead(lead: Lead, store: LeadStoreBase, force_research: bool = False) -> Lead:
    if lead.draft_locked:
        raise ValueError(f"Lead {lead.id} draft is locked (already approved/sent).")
    draft, note = _initial_draft(lead, force_research)
    _snapshot_current_draft(lead, DraftVersionSource.initial, "Draft regenerated")
    lead.current_draft_subject = draft.subject
    lead.current_draft_body_html = draft.body_html
    lead.personalization_note = note
    store.save(lead)
    return lead


def generate_drafts_for_leads(leads: list[Lead], store: LeadStoreBase) -> tuple[list[Lead], list[dict]]:
    def one(lead: Lead):
        try:
            return generate_draft_for_lead(lead, store), None
        except Exception as e:  # noqa: BLE001
            return None, {"lead_id": lead.id, "error": str(e)}

    with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_DRAFTS) as pool:
        results = list(pool.map(one, leads))  # pool.map keeps the input order
    updated = [lead for lead, _ in results if lead is not None]
    errors = [err for _, err in results if err is not None]
    return updated, errors


def apply_manual_edit(lead: Lead, subject: str, body_html: str, store: LeadStoreBase) -> Lead:
    """Staff hand-edits the draft directly (§6.3)."""
    if lead.draft_locked:
        raise ValueError(f"Lead {lead.id} draft is locked (already approved/sent).")
    _snapshot_current_draft(lead, DraftVersionSource.manual, "Manual edit")
    lead.current_draft_subject = subject
    lead.current_draft_body_html = body_html
    store.save(lead)
    return lead


def apply_chatbot_edit(lead: Lead, instruction: str, store: LeadStoreBase) -> Lead:
    """Staff requests a rewrite via the chatbot, powered by GPT-4o (§6, §9)."""
    if lead.draft_locked:
        raise ValueError(f"Lead {lead.id} draft is locked (already approved/sent).")
    current = llm_service.DraftOutput(
        subject=lead.current_draft_subject or "", body_html=lead.current_draft_body_html or ""
    )
    revised = llm_service.apply_chatbot_modification(current, instruction)
    _snapshot_current_draft(lead, DraftVersionSource.chatbot, "Chatbot rewrite")
    lead.current_draft_subject = revised.subject
    lead.current_draft_body_html = revised.body_html
    store.save(lead)
    return lead


def restore_draft_version(lead: Lead, version_index: int, store: LeadStoreBase) -> Lead:
    """Staff picks an earlier version from the dropdown -- makes it current again."""
    if lead.draft_locked:
        raise ValueError(f"Lead {lead.id} draft is locked (already approved/sent).")
    if version_index < 0 or version_index >= len(lead.draft_history):
        raise ValueError(f"No draft version at index {version_index}.")
    version = lead.draft_history[version_index]
    _snapshot_current_draft(lead, DraftVersionSource.manual, "Manual edit")
    lead.current_draft_subject = version.subject
    lead.current_draft_body_html = version.body_html
    store.save(lead)
    return lead
