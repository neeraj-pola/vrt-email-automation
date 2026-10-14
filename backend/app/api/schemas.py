"""Request/response bodies for the API that aren't just the Lead model itself."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.lead import QualifiedStatus, QualifiedStatusSource


class ImportResult(BaseModel):
    imported: int
    skipped: int
    skipped_detail: list[dict]


class ImportColumnField(BaseModel):
    field: str
    label: str
    required: bool
    matched_column: Optional[str] = None


class ImportPreviewResult(BaseModel):
    columns: list[str]
    fields: list[ImportColumnField]


class LeadIdsRequest(BaseModel):
    lead_ids: list[str]


class ManualDraftEditRequest(BaseModel):
    subject: str
    body_html: str


class ChatbotEditRequest(BaseModel):
    instruction: str


class ExportRequest(BaseModel):
    lead_ids: list[str]
    label: str = "leads"


class RestoreVersionRequest(BaseModel):
    version_index: int


class PreviewDraftRequest(BaseModel):
    # Both optional -- omit to preview the current draft, or supply both to
    # preview arbitrary content (e.g. a superseded draft_history entry).
    subject: Optional[str] = None
    body_html: Optional[str] = None


class AdvisorChatTurn(BaseModel):
    role: str  # "staff" | "assistant"
    text: str


class AdvisorAskRequest(BaseModel):
    question: str
    history: list[AdvisorChatTurn] = []


class AdvisorAskResponse(BaseModel):
    answer: str


class QualifiedStatusOverrideRequest(BaseModel):
    qualified_status: QualifiedStatus
    reasoning: Optional[str] = None


class ManualFieldsUpdateRequest(BaseModel):
    notes: Optional[str] = None
    crm_status: Optional[str] = None
    sale_status: Optional[str] = None


class ConfirmCallRequest(BaseModel):
    scheduled_call_time: datetime


class TickResult(BaseModel):
    lead_id: str
    action: str


class PipelineConfigResponse(BaseModel):
    follow_up_wait_days: int
    max_follow_ups: int
    daily_send_cap: int


class SendBatchResult(BaseModel):
    sent: list[str]
    deferred_to_next_batch: list[str]
    errors: list[dict]
    daily_cap: int
    already_sent_today: int


class SendStatsResponse(BaseModel):
    sent_today: int
    daily_cap: int
