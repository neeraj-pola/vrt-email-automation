"""
Regression test: Lead keeps created_time/last_sent_at/scheduled_call_time as
naive UTC internally (everything else compares them against naive
datetime.utcnow()), but serialized to JSON with no UTC marker, the browser's
`new Date(...)` parses them as LOCAL time instead of UTC -- a lead sent at
15:34 UTC showed as "03:34 PM" in a browser already in EST, hours ahead of
when it actually happened. Every datetime field going out in JSON must carry
an explicit UTC offset.
"""
import json
from datetime import datetime

from app.models.lead import DraftVersion, DraftVersionSource, Lead


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime(2026, 9, 1, 15, 34, 0),  # naive, as stored internally
        full_name="Test Lead",
        email="test.lead@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_created_time_serializes_with_utc_offset():
    lead = make_lead()
    data = json.loads(lead.model_dump_json())
    assert data["created_time"] == "2026-09-01T15:34:00+00:00"


def test_last_sent_at_serializes_with_utc_offset():
    lead = make_lead(last_sent_at=datetime(2026, 9, 1, 15, 34, 0))
    data = json.loads(lead.model_dump_json())
    assert data["last_sent_at"] == "2026-09-01T15:34:00+00:00"


def test_null_last_sent_at_stays_null():
    lead = make_lead(last_sent_at=None)
    data = json.loads(lead.model_dump_json())
    assert data["last_sent_at"] is None


def test_internal_value_stays_naive_for_comparisons():
    """The fix must only affect JSON output, not break naive-datetime math elsewhere."""
    lead = make_lead(last_sent_at=datetime(2026, 9, 1, 15, 34, 0))
    assert lead.last_sent_at.tzinfo is None
    assert (datetime.utcnow() > lead.last_sent_at) or True  # doesn't raise TypeError


def test_draft_version_created_at_serializes_with_utc_offset():
    lead = make_lead()
    lead.draft_history.append(DraftVersion(subject="s", body_html="b", source=DraftVersionSource.chatbot))
    data = json.loads(lead.model_dump_json())
    assert data["draft_history"][0]["created_at"].endswith("Z") or "+00:00" in data["draft_history"][0]["created_at"]
