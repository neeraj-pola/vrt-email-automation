"""
Lead import, listing, and manual-field endpoints.
Covers §6 stage 1 (import & dedup) and the staff-only manual fields (§4).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile

from app.api.deps import get_store, require_staff
from app.api.schemas import (
    ConfirmCallRequest,
    ExportRequest,
    ImportPreviewResult,
    ImportResult,
    ManualFieldsUpdateRequest,
    QualifiedStatusOverrideRequest,
)
from app.models.lead import EmailStatus, Lead, QualifiedStatusSource
from app.pipeline.import_leads import ImportError_, import_leads_from_excel, preview_excel_columns
from app.pipeline.export_leads import leads_to_xlsx
from app.pipeline.outreach_graph import stop_outreach_permanently
from app.services import company_research, llm_service
from app.services.company_research import CompanyProfile
from app.services.email_template import render_email_html
from app.services.gmail_service import get_gmail_client
from app.services.storage_service import LeadStoreBase

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("/import/preview", response_model=ImportPreviewResult)
async def import_preview(file: UploadFile, _staff=Depends(require_staff)):
    """
    Reads just the header row and returns every logical field alongside
    whichever actual column name auto-matched it (or null, if none did) --
    the staff-facing "does this file's column naming match what we expect"
    step (§3), shown before anything is actually imported. Confirm or
    correct each field, then POST /import with that mapping.
    """
    suffix = Path(file.filename or "leads.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        return preview_excel_columns(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.post("/import", response_model=ImportResult)
async def import_leads(file: UploadFile, column_map: str | None = Form(None),
                        store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    column_map: optional JSON object (logical field -> actual column name),
    staff-confirmed via /import/preview -- overrides the automatic COLUMN_MAP
    guess for whichever fields it names. Omit entirely to fall back to pure
    auto-detection, unchanged from before this existed.
    """
    suffix = Path(file.filename or "leads.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    overrides = None
    if column_map:
        try:
            overrides = json.loads(column_map)
        except json.JSONDecodeError as e:
            Path(tmp_path).unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"column_map is not valid JSON: {e}") from e

    try:
        leads, skipped = import_leads_from_excel(tmp_path, column_overrides=overrides)
    except ImportError_ as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    added = store.upsert_many(leads)
    return ImportResult(imported=added, skipped=len(skipped), skipped_detail=skipped)


@router.get("", response_model=list[Lead])
async def list_leads(store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Dense list view data (§9): every lead with current status. Uses the
    light fetch -- the list view never shows/needs draft_history or the
    full draft HTML body (only current_draft_subject, for the "Draft ready"
    stage label), and this is a read-only view, never saved back, so
    there's no hydration concern here the way there is in tick.py.
    """
    return store.list_all_light()


@router.post("/export")
async def export_leads(body: ExportRequest, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Excel download of exactly the leads the dashboard is showing (its current
    company / stage / status / search filters), sent as a list of ids so the
    file always matches what staff are looking at. Declared before
    /{lead_id} so "export" isn't read as a lead id.
    """
    wanted = set(body.lead_ids)
    leads = [lead for lead in store.list_all_light() if lead.id in wanted]
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in body.label)[:60] or "leads"
    return Response(
        content=leads_to_xlsx(leads, body.label),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_label}.xlsx"'},
    )


@router.get("/{lead_id}", response_model=Lead)
async def get_lead(lead_id: str, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.patch("/{lead_id}/manual-fields", response_model=Lead)
async def update_manual_fields(lead_id: str, body: ManualFieldsUpdateRequest,
                                store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """notes, crm_status, sale_status -- staff-only fields, never AI-touched (§4)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if body.notes is not None:
        lead.notes = body.notes
    if body.crm_status is not None:
        lead.crm_status = body.crm_status
    if body.sale_status is not None:
        lead.sale_status = body.sale_status
    store.save(lead)
    return lead


@router.post("/{lead_id}/qualified-status", response_model=Lead)
async def override_qualified_status(lead_id: str, body: QualifiedStatusOverrideRequest,
                                     store: LeadStoreBase = Depends(get_store), staff=Depends(require_staff)):
    """Staff confirms or overrides the AI-proposed qualification (§4, §5, §6.7)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead.qualified_status = body.qualified_status
    lead.qualified_status_source = QualifiedStatusSource.staff
    if body.reasoning is not None:
        lead.qualified_status_reasoning = body.reasoning
    store.save(lead)
    return lead


@router.post("/{lead_id}/confirm-call", response_model=Lead)
async def confirm_call(lead_id: str, body: ConfirmCallRequest,
                        store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """
    Once staff confirm a time with the lead (manual, per §12 -- no calendar
    integration this phase), record it and send the lead a confirmation email.
    """
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.scheduled_call_time = body.scheduled_call_time
    gmail = get_gmail_client()
    confirmation = llm_service.generate_confirmation_email(lead, body.scheduled_call_time.isoformat())
    rendered_html = render_email_html(lead, confirmation.subject, confirmation.body_html)
    gmail.send(lead.email, confirmation.subject, rendered_html, thread_id=lead.thread_id)
    store.save(lead)
    return lead


@router.post("/{lead_id}/unsubscribe", response_model=Lead)
async def unsubscribe_lead(lead_id: str, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """Manual override endpoint mirroring the automatic unsubscribe-keyword detection (§8)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    stop_outreach_permanently(lead, EmailStatus.unsubscribed)
    store.save(lead)
    return lead


@router.get("/{lead_id}/company-profile", response_model=CompanyProfile | None)
async def get_company_profile(lead_id: str, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """The cached research on this lead's company, or null if none has been done yet. Never triggers research."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return company_research.get_cached(lead)


@router.post("/{lead_id}/company-profile/refresh", response_model=CompanyProfile | None)
async def refresh_company_profile(lead_id: str, store: LeadStoreBase = Depends(get_store), _staff=Depends(require_staff)):
    """Re-runs the research for this lead's company (costs a search credit if the email domain doesn't work)."""
    lead = store.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not (lead.company or "").strip():
        raise HTTPException(status_code=409, detail="This lead has no company on file to research.")
    return await asyncio.to_thread(company_research.get_or_research, lead, True)
