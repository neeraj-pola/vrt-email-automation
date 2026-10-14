"""
Stage 1: Import & dedup (CLAUDE.md §6.1, §3).

- Column names are NOT hardcoded positionally: we map by a configurable
  column-name lookup (COLUMN_MAP), so a shifted ad-platform export can be
  fixed by editing the lookup, not the code.
- Fails loudly if a required field is missing from the sheet, rather than
  silently importing bad/incomplete data.
- Dedupes by email: a lead resubmitting the same form is not double-counted
  or contacted twice.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from app.models.lead import Lead, QualifiedStatus, QualifiedStatusSource
from app.services import llm_service

# Configurable column-name lookup: logical field -> possible source header names.
# Add alternate header spellings here as ad-platform exports drift, without
# touching import logic.
COLUMN_MAP: dict[str, list[str]] = {
    "first_name": ["first name", "first_name", "firstname"],
    "last_name": ["last name", "last_name", "lastname"],
    "linkedin_url": ["url", "linkedin url", "profile url", "linkedin_url"],
    "email": ["email address", "email", "e-mail"],
    "company": ["company", "company name", "organization"],
    "position": ["position", "title", "job title"],
    "created_time": ["connected on", "connected_on", "created_time"],
    "employee_count": ["employee count", "employee_count", "company size", "employees"],
    "location": ["location", "city", "region"],
    # Optional: a lead sheet can arrive with real prior work already in it
    # (a company was tracking outreach manually before this system existed).
    # Not required, and no candidate is likely to auto-match by accident --
    # staff map it explicitly in the /import/preview step when it applies.
    "notes": ["notes", "staff notes", "qualification notes", "notes / why not qualified"],
}

# Fields the pipeline cannot function without. Everything else degrades to
# None/unknown rather than failing the whole import.
REQUIRED_FIELDS = ["first_name", "email"]


class ImportError_(Exception):
    """Raised when the source sheet is missing a required, mappable column."""


# Human-readable label per logical field, for the staff-facing column
# confirmation step (routes_leads.py's /import/preview) -- shown next to
# each auto-detected match so staff can confirm or correct it before
# anything is actually imported.
FIELD_LABELS: dict[str, str] = {
    "first_name": "First name",
    "last_name": "Last name",
    "linkedin_url": "Profile URL",
    "email": "Email address",
    "company": "Company",
    "position": "Position",
    "created_time": "Connected on",
    "employee_count": "Employee count",
    "location": "Location",
    "notes": "Notes (optional -- pre-existing staff notes, if this sheet has them)",
}


def _resolve_column(df_columns: list[str], candidates: list[str]) -> Optional[str]:
    lower_map = {c.strip().lower(): c for c in df_columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def resolve_all_columns(
    df_columns: list[str], overrides: Optional[dict[str, str]] = None
) -> dict[str, Optional[str]]:
    """
    Resolves every logical field to an actual column name in this file.
    An override (staff-confirmed mapping from the /import/preview step)
    wins for that field; otherwise falls back to the COLUMN_MAP guess.
    An override naming a column that isn't actually in the file resolves
    to None -- same as no match -- so a bad manual entry fails the same
    loud "missing required column" check as an unmapped one, rather than
    a confusing KeyError deep in the row loop.
    """
    lower_map = {c.strip().lower(): c for c in df_columns}
    resolved: dict[str, Optional[str]] = {}
    for field, candidates in COLUMN_MAP.items():
        override = (overrides or {}).get(field)
        if override:
            resolved[field] = lower_map.get(override.strip().lower())
        else:
            resolved[field] = _resolve_column(df_columns, candidates)
    return resolved


def preview_excel_columns(file_path: str, overrides: Optional[dict[str, str]] = None) -> dict:
    """
    Reads only the header row (nrows=0 -- no row data parsed) so staff can
    confirm/correct the column mapping before anything is actually
    imported. Column naming isn't guaranteed to stay identical across
    future ad-platform exports (§3) -- this is the "does the file's naming
    match what we expect" check, surfaced as a real UI step instead of a
    silent guess or a hardcoded COLUMN_MAP edit.
    """
    df_columns = list(pd.read_excel(file_path, nrows=0).columns)
    resolved = resolve_all_columns(df_columns, overrides)
    return {
        "columns": df_columns,
        "fields": [
            {
                "field": field,
                "label": FIELD_LABELS[field],
                "required": field in REQUIRED_FIELDS,
                "matched_column": resolved.get(field),
            }
            for field in COLUMN_MAP
        ],
    }


def _as_count(val) -> Optional[str]:
    """Employee count arrives as a number (150, 150.0) or a range ("51-200")."""
    if val is None:
        return None
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip() or None


def import_leads_from_excel(
    file_path: str, column_overrides: Optional[dict[str, str]] = None
) -> tuple[list[Lead], list[dict]]:
    """
    Returns (leads, skipped_rows). skipped_rows carries the original row data
    plus a reason, for anything dropped as an unparseable/duplicate row --
    nothing is silently discarded without a trace.

    column_overrides: staff-confirmed mapping (logical field -> actual
    column name in this file) from the /import/preview step, for a source
    sheet whose headers don't match COLUMN_MAP's guesses. Any field not in
    here still falls back to the normal auto-detection.
    """
    df = pd.read_excel(file_path)
    df_columns = list(df.columns)

    resolved = resolve_all_columns(df_columns, column_overrides)

    missing_required = [f for f in REQUIRED_FIELDS if resolved.get(f) is None]
    if missing_required:
        raise ImportError_(
            f"Source sheet is missing required column(s): {missing_required}. "
            f"Available columns: {df_columns}. "
            f"Update COLUMN_MAP in app/pipeline/import_leads.py if the export "
            f"renamed these fields."
        )

    leads: list[Lead] = []
    skipped: list[dict] = []
    seen_emails: set[str] = set()

    for idx, row in df.iterrows():
        email_col = resolved["email"]
        raw_email = row.get(email_col)
        if pd.isna(raw_email) or not str(raw_email).strip():
            skipped.append({"row": idx, "reason": "missing email", "data": row.to_dict()})
            continue
        email = str(raw_email).strip().lower()

        if email in seen_emails:
            skipped.append({"row": idx, "reason": "duplicate email", "data": row.to_dict()})
            continue

        def get(field: str):
            col = resolved.get(field)
            if col is None:
                return None
            val = row.get(col)
            return None if pd.isna(val) else val

        first_name = _as_str(get("first_name"))
        last_name = _as_str(get("last_name"))
        full_name = " ".join(part for part in (first_name, last_name) if part)
        if not full_name:
            skipped.append({"row": idx, "reason": "missing name", "data": row.to_dict()})
            continue

        created_raw = get("created_time")
        try:
            created_time = pd.to_datetime(created_raw) if created_raw is not None else datetime.utcnow()
        except Exception:
            created_time = datetime.utcnow()

        lead = Lead(
            created_time=created_time,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            email=email,
            linkedin_url=_as_str(get("linkedin_url")),
            company=_as_str(get("company")),
            position=_as_str(get("position")),
            employee_count=_as_count(get("employee_count")),
            location=_as_str(get("location")),
            notes=_as_str(get("notes")),
        )
        if lead.notes:
            _propose_qualification_from_notes(lead)
        leads.append(lead)
        seen_emails.add(email)

    return leads, skipped


def _propose_qualification_from_notes(lead: Lead) -> None:
    """
    A mapped "Notes" column can carry real prior work (§4's AI-proposed/
    staff-confirmable qualified_status exists for exactly this kind of
    signal) -- e.g. "called 8/12, renter, not interested" shouldn't just
    sit there as inert text when it's exactly the kind of thing the AI
    already knows how to weigh (§5) once a lead replies. This runs the same
    judgment at import time instead, from the note rather than a reply.

    Best-effort: one bad/slow LLM call on one row must not fail an entire
    600-lead import over a single note the model couldn't parse.
    """
    try:
        result = llm_service.propose_qualification_from_import_notes(lead, lead.notes)
    except Exception:  # noqa: BLE001
        return
    if result.qualified_status:
        lead.qualified_status = QualifiedStatus(result.qualified_status)
        lead.qualified_status_source = QualifiedStatusSource.ai
        lead.qualified_status_reasoning = result.reasoning


def _as_str(val) -> Optional[str]:
    return None if val is None else str(val).strip()
