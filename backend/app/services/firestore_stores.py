"""
Firestore-backed stores. Collections (each prefixed by FIRESTORE_COLLECTION_PREFIX):

  leads/{lead id}                one document per lead (see below)
  pipeline_state/gmail_sync      {gmail_history_id}
  pipeline_state/follow_up_schedule   {default_days, by_stage, by_company: [{company, days}]}
  company_profiles/{sha1(key)}   {key, profile}  -- one per company, shared by its leads
  draft_changes/state            {version, changes[], saved[]}  -- the Modify draft page

Everything goes through this backend with the Admin SDK; the web client never
talks to Firestore directly (firestore.rules denies it), so access control
lives in one place: require_staff in app/api/deps.py.

How a Lead maps to a document (nothing from the old SQL schema is lost):
  - every Lead field is stored under its own name; enums as their string values;
    timestamps as native Firestore Timestamps (queryable), nested draft_history
    as an array of maps (timestamps there stay ISO strings -- never queried)
  - two derived fields are added on every save, purely so queries work:
      email_lower          -> dedupe and "find this sender" without scanning
      follow_up_due_at     -> when a waiting lead is next due (absent otherwise),
                              so a tick reads only the due leads
Firestore bills per document read, which is why those two exist.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import settings
from app.models.draft_changes import DraftChangeState
from app.models.lead import Lead
from app.services import schedule_service
from app.services.firebase_app import get_firestore_client
from app.services.storage_service import (
    LIGHT_FIELDS,
    CompanyProfileStore,
    DraftChangeConflict,
    DraftChangeStore,
    LeadStoreBase,
    PipelineStateStore,
    _row_to_lead,
)

# Indirection so tests can swap the transaction decorator for a plain call.
_transactional = firestore.transactional

_IN_QUERY_LIMIT = 30  # Firestore's cap on values in one `in` filter
_BATCH_LIMIT = 400  # Firestore's cap is 500 writes per batch
_CONTACTED = ["sent", "bounced", "unsubscribed", "exhausted"]
_TIMESTAMP_FIELDS = (
    "created_time", "last_sent_at", "scheduled_call_time", "last_reply_at",
    "website_clicked_at", "get_started_clicked_at",
)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Naive UTC (the codebase's convention) -> timezone-aware, which Firestore stores as a Timestamp."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def lead_to_document(lead: Lead) -> dict:
    doc = lead.model_dump(mode="json")  # enums -> values, nested models -> plain dicts
    for field in _TIMESTAMP_FIELDS:
        doc[field] = _utc(getattr(lead, field))
    doc["email_lower"] = lead.email.lower()
    doc["follow_up_due_at"] = _utc(schedule_service.due_at(lead))
    return doc


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _eq(field: str, value):
    return FieldFilter(field, "==", value)


class FirestoreLeadStore(LeadStoreBase):
    def __init__(self, db=None):
        self._db = db or get_firestore_client()
        self._col = self._db.collection(settings.collection("leads"))

    def save(self, lead: Lead) -> None:
        self._col.document(lead.id).set(lead_to_document(lead))

    def get(self, lead_id: str) -> Optional[Lead]:
        snap = self._col.document(lead_id).get()
        return _row_to_lead(snap.to_dict()) if snap.exists else None

    def _first(self, query) -> Optional[Lead]:
        for snap in query.limit(1).stream():
            return _row_to_lead(snap.to_dict())
        return None

    def get_by_thread_id(self, thread_id: str) -> Optional[Lead]:
        return self._first(self._col.where(filter=_eq("thread_id", thread_id)))

    def find_by_email(self, email: str) -> Optional[Lead]:
        return self._first(self._col.where(filter=_eq("email_lower", email.strip().lower())))

    def upsert_many(self, leads: list[Lead]) -> int:
        # Dedupe inside the batch first (first occurrence wins), then ask Firestore
        # which of the remaining addresses already exist -- 30 per query, not one
        # query per lead.
        unique: dict[str, Lead] = {}
        for lead in leads:
            unique.setdefault(lead.email.lower(), lead)
        existing: set[str] = set()
        for chunk in _chunks(list(unique), _IN_QUERY_LIMIT):
            query = self._col.where(filter=FieldFilter("email_lower", "in", chunk)).select(["email_lower"])
            existing.update(snap.get("email_lower") for snap in query.stream())
        new = [lead for email, lead in unique.items() if email not in existing]
        for chunk in _chunks(new, _BATCH_LIMIT):
            batch = self._db.batch()
            for lead in chunk:
                batch.set(self._col.document(lead.id), lead_to_document(lead))
            batch.commit()
        return len(new)

    def list_all(self) -> list[Lead]:
        return [_row_to_lead(snap.to_dict()) for snap in self._col.stream()]

    def list_all_light(self) -> list[Lead]:
        # A field mask: the heavy fields never leave Firestore. (Billing is per
        # document either way; this saves bandwidth and parsing.)
        return [_row_to_lead(snap.to_dict()) for snap in self._col.select(LIGHT_FIELDS).stream()]

    def list_due_for_follow_up(self, now: datetime) -> list[Lead]:
        query = self._col.where(filter=FieldFilter("follow_up_due_at", "<=", _utc(now)))
        return [_row_to_lead(snap.to_dict()) for snap in query.stream()]

    def list_contacted_light(self) -> list[Lead]:
        query = self._col.where(filter=FieldFilter("email_status", "in", _CONTACTED)).select(LIGHT_FIELDS)
        return [_row_to_lead(snap.to_dict()) for snap in query.stream()]

    def has_contacted(self) -> bool:
        query = self._col.where(filter=FieldFilter("email_status", "in", _CONTACTED)).select(["email_status"])
        return any(True for _ in query.limit(1).stream())

    def count_sent_since(self, since: datetime) -> int:
        # An aggregation query: billed per 1,000 matching index entries, not per lead.
        result = self._col.where(filter=FieldFilter("last_sent_at", ">=", _utc(since))).count().get()
        return int(result[0][0].value)

    def reindex_follow_up_due(self) -> int:
        changed = 0
        for snap in self._col.where(filter=_eq("email_status", "sent")).stream():
            data = snap.to_dict()
            wanted = _utc(schedule_service.due_at(_row_to_lead(data)))
            if data.get("follow_up_due_at") != wanted:
                snap.reference.update({"follow_up_due_at": wanted})
                changed += 1
        return changed


class FirestorePipelineStateStore(PipelineStateStore):
    def __init__(self, db=None):
        self._db = db or get_firestore_client()
        self._col = self._db.collection(settings.collection("pipeline_state"))
        self._cursor = self._col.document("gmail_sync")
        self._schedule = self._col.document("follow_up_schedule")

    def get_gmail_history_id(self) -> Optional[str]:
        snap = self._cursor.get()
        return (snap.to_dict() or {}).get("gmail_history_id") if snap.exists else None

    def set_gmail_history_id(self, history_id: str, expected_previous: Optional[str] = None) -> bool:
        if expected_previous is None:
            # Bootstrap (or a caller that deliberately doesn't want CAS
            # semantics) -- nothing to compare against yet, so a plain write.
            self._cursor.set({"gmail_history_id": history_id}, merge=True)
            return True
        ref = self._cursor

        # A real Firestore transaction: the read and the write are atomic, so
        # two overlapping ticks can't both advance from the same old value.
        @_transactional
        def swap(transaction) -> bool:
            snap = ref.get(transaction=transaction)
            current = (snap.to_dict() or {}).get("gmail_history_id") if snap.exists else None
            if current != expected_previous:
                return False
            transaction.set(ref, {"gmail_history_id": history_id}, merge=True)
            return True

        return swap(self._db.transaction())

    def get_schedule(self) -> Optional[dict]:
        snap = self._schedule.get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        # Company names are stored as a list, not as map keys: a company name can
        # contain characters (".", "/", leading underscores) that are awkward or
        # illegal as Firestore field names.
        return {
            "default_days": data.get("default_days"),
            "by_stage": data.get("by_stage") or {},
            "by_company": {row["company"]: row["days"] for row in data.get("by_company") or []},
        }

    def set_schedule(self, schedule: dict) -> None:
        self._schedule.set({
            "default_days": schedule["default_days"],
            "by_stage": schedule.get("by_stage") or {},
            "by_company": [{"company": c, "days": d} for c, d in (schedule.get("by_company") or {}).items()],
        })


def _profile_doc_id(key: str) -> str:
    # A company name can contain "/" (illegal in a document id) -- hash it; the
    # readable key is stored inside the document.
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


class FirestoreCompanyProfileStore(CompanyProfileStore):
    def __init__(self, db=None):
        self._col = (db or get_firestore_client()).collection(settings.collection("company_profiles"))

    def get(self, key: str):
        from app.services.company_research import CompanyProfile

        snap = self._col.document(_profile_doc_id(key)).get()
        return CompanyProfile(**snap.to_dict()["profile"]) if snap.exists else None

    def save(self, profile) -> None:
        self._col.document(_profile_doc_id(profile.key)).set({"key": profile.key, "profile": profile.model_dump(mode="json")})


class FirestoreDraftChangeStore(DraftChangeStore):
    def __init__(self, db=None):
        self._db = db or get_firestore_client()
        self._ref = self._db.collection(settings.collection("draft_changes")).document("state")

    def get_state(self) -> DraftChangeState:
        snap = self._ref.get()
        return DraftChangeState(**snap.to_dict()) if snap.exists else DraftChangeState()

    def put_state(self, state: DraftChangeState, expected_version: int) -> DraftChangeState:
        ref = self._ref
        new_state = state.model_copy(update={"version": expected_version + 1})

        @_transactional
        def write(transaction) -> None:
            snap = ref.get(transaction=transaction)
            current = (snap.to_dict() or {}).get("version", 0) if snap.exists else 0
            if current != expected_version:
                raise DraftChangeConflict()
            transaction.set(ref, new_state.model_dump(mode="json"))

        write(self._db.transaction())
        return new_state
