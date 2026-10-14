from app.pipeline.import_leads import ImportError_, import_leads_from_excel, preview_excel_columns
from app.services import llm_service

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def synthetic_sheet(tmp_path):
    """The synthetic sheet is built from its generator, so the suite needs no checked-in spreadsheet."""
    spec = importlib.util.spec_from_file_location(
        "generate_synthetic_leads", Path(__file__).resolve().parents[1] / "sample_data" / "generate_synthetic_leads.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "synthetic_leads.xlsx"
    pd.DataFrame(module.ROWS).to_excel(path, index=False)
    return str(path)


def test_import_dedupes_and_maps_columns(synthetic_sheet):
    leads, skipped = import_leads_from_excel(synthetic_sheet)
    emails = [l.email for l in leads]
    assert len(emails) == len(set(emails)), "emails must be deduped"
    assert "priya.shah.test@example.com" in emails
    assert len(leads) == 5  # 6 rows, 1 duplicate
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "duplicate email"

    maria = next(l for l in leads if l.email == "maria.gonzalez.test@example.com")
    assert maria.full_name == "Maria Gonzalez"
    assert (maria.first_name, maria.last_name) == ("Maria", "Gonzalez")
    assert maria.company == "Acme Industrial"
    assert maria.position == "CEO"
    assert maria.employee_count == "120"  # numeric cell -> clean text, not "120.0"
    assert maria.location == "Stamford, CT"
    assert maria.linkedin_url == "https://www.linkedin.com/in/maria-gonzalez-test"
    assert maria.created_time.year == 2026  # "Connected On"


def test_import_fails_loudly_on_missing_required_column(tmp_path):
    import pandas as pd

    bad_file = tmp_path / "bad_leads.xlsx"
    pd.DataFrame([{"First Name": "No Email Person"}]).to_excel(bad_file, index=False)

    try:
        import_leads_from_excel(str(bad_file))
        assert False, "expected ImportError_"
    except ImportError_ as e:
        assert "email" in str(e).lower()


def test_import_column_name_drift_is_configurable():
    """
    Column lookup should tolerate case/whitespace drift without code changes
    -- exercises the configurable-lookup requirement from CLAUDE.md §3.
    """
    import pandas as pd
    import tempfile

    df = pd.DataFrame([{
        "Email": "case.drift.test@example.com",
        "First Name": "Case Drift",
        "created_time": "2026-08-01",
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        leads, _ = import_leads_from_excel(tmp.name)
    assert len(leads) == 1
    assert leads[0].email == "case.drift.test@example.com"


def test_preview_reports_matched_and_unmatched_columns():
    """
    The staff-facing column-confirmation step (§3): before anything is
    imported, show every logical field alongside whatever column name
    auto-matched it, or null if the file's naming has drifted too far for
    COLUMN_MAP to catch on its own.
    """
    import pandas as pd
    import tempfile

    df = pd.DataFrame([{
        "Email Address": "preview.test@example.com",
        "First Name": "Preview",
        "Last Name": "Test",
        "Headcount Band": "51-200",  # doesn't match any known employee-count candidate
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        result = preview_excel_columns(tmp.name)

    assert "Headcount Band" in result["columns"]
    by_field = {f["field"]: f for f in result["fields"]}
    assert by_field["email"]["matched_column"] == "Email Address"
    assert by_field["first_name"]["matched_column"] == "First Name"
    assert by_field["email"]["required"] is True
    assert by_field["employee_count"]["matched_column"] is None  # genuinely unmatched
    assert by_field["employee_count"]["required"] is False


def test_import_uses_staff_confirmed_column_override():
    """
    A staff-confirmed mapping for a field COLUMN_MAP couldn't auto-match
    (e.g. "Headcount Band" instead of "Employee Count") must be honored, without
    needing a code change to COLUMN_MAP itself.
    """
    import pandas as pd
    import tempfile

    df = pd.DataFrame([{
        "Email Address": "override.test@example.com",
        "First Name": "Override",
        "Last Name": "Test",
        "Headcount Band": "51-200",
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        leads, _ = import_leads_from_excel(tmp.name, column_overrides={"employee_count": "Headcount Band"})

    assert len(leads) == 1
    assert leads[0].employee_count == "51-200"


def test_imported_notes_propose_qualification_via_ai(monkeypatch):
    """
    A lead sheet can arrive with real prior work already in it -- a "Notes"
    column from before this system existed. If it contains a real
    qualification signal, the AI should propose qualified_status from it
    at import time (§4/§5), same source="ai" as any other AI proposal, not
    just sit there as inert text.
    """
    import pandas as pd
    import tempfile

    monkeypatch.setattr(
        llm_service, "propose_qualification_from_import_notes",
        lambda lead, notes: llm_service.ImportNotesQualification(
            qualified_status="not_qualified", reasoning="Renter, per staff note."
        ),
    )
    df = pd.DataFrame([{
        "First Name": "Notes Test", "Email Address": "notes.test@example.com",
        "notes": "Called 8/12 -- renter, not interested.",
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        leads, _ = import_leads_from_excel(tmp.name)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.notes == "Called 8/12 -- renter, not interested."
    assert lead.qualified_status.value == "not_qualified"
    assert lead.qualified_status_source.value == "ai"
    assert lead.qualified_status_reasoning == "Renter, per staff note."


def test_imported_notes_with_no_qualification_signal_leave_status_unset(monkeypatch):
    import pandas as pd
    import tempfile

    monkeypatch.setattr(
        llm_service, "propose_qualification_from_import_notes",
        lambda lead, notes: llm_service.ImportNotesQualification(qualified_status=None, reasoning=None),
    )
    df = pd.DataFrame([{
        "First Name": "Vague Notes", "Email Address": "vague.notes@example.com",
        "notes": "Call back Tuesday.",
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        leads, _ = import_leads_from_excel(tmp.name)

    assert leads[0].notes == "Call back Tuesday."
    assert leads[0].qualified_status.value == "unset"


def test_a_failed_qualification_call_does_not_fail_the_whole_import(monkeypatch):
    """One row's LLM hiccup must not sink an entire 600-lead import."""
    import pandas as pd
    import tempfile

    def boom(lead, notes):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(llm_service, "propose_qualification_from_import_notes", boom)
    df = pd.DataFrame([{
        "First Name": "Resilience Test", "Email Address": "resilience@example.com",
        "notes": "Some prior note.",
    }])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        leads, skipped = import_leads_from_excel(tmp.name)

    assert len(leads) == 1
    assert leads[0].notes == "Some prior note."
    assert leads[0].qualified_status.value == "unset"  # left alone, not crashed


def test_override_naming_a_nonexistent_column_resolves_to_unmatched_not_a_crash():
    """A bad manual override (typo, wrong file) must fail the same loud
    'missing required column' path as no match at all -- never a raw
    KeyError from deep in the row loop."""
    import pandas as pd
    import tempfile

    df = pd.DataFrame([{"First Name": "Typo Test"}])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        df.to_excel(tmp.name, index=False)
        try:
            import_leads_from_excel(tmp.name, column_overrides={"email": "Emial"})
            assert False, "expected ImportError_"
        except ImportError_ as e:
            assert "email" in str(e).lower()
