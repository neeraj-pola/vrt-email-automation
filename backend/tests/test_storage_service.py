"""
Regression coverage: stored timestamps come back timezone-aware (Firestore
Timestamps, or ISO strings), but the rest of the pipeline (outreach_graph,
send_batch) compares against naive datetime.utcnow(). Without normalization
at the storage boundary, that comparison raises TypeError the first time a
lead round-trips through storage -- caught live when /pipeline/tick crashed
on a real lead.
"""
from app.services.storage_service import _row_to_lead


def make_row(**overrides):
    row = {
        "id": "abc-123",
        "created_time": "2026-08-31T20:35:48.871000+00:00",
        "full_name": "Test Lead",
        "email": "test@example.com",
        "email_status": "sent",
        "follow_up_count": 0,
        "qualified_status": "unset",
        "draft_locked": False,
        "last_sent_at": None,
    }
    row.update(overrides)
    return row


def test_aware_timestamp_from_storage_normalized_to_naive_utc():
    lead = _row_to_lead(make_row(last_sent_at="2026-08-31T20:36:08.144338Z"))
    assert lead.last_sent_at.tzinfo is None


def test_naive_or_missing_timestamp_passes_through():
    lead = _row_to_lead(make_row(last_sent_at=None))
    assert lead.last_sent_at is None


def test_created_time_normalized():
    lead = _row_to_lead(make_row())
    assert lead.created_time.tzinfo is None


def test_firestore_timestamps_are_normalized_too():
    from datetime import datetime, timezone

    lead = _row_to_lead(make_row(last_sent_at=datetime(2026, 8, 31, 20, 36, 8, tzinfo=timezone.utc),
                                 website_clicked_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)))
    assert lead.last_sent_at.tzinfo is None and lead.website_clicked_at.tzinfo is None


def test_firestore_datetime_subclass_becomes_a_plain_datetime_that_can_be_written_back():
    """
    Found against real Firestore: reads return DatetimeWithNanoseconds, and keeping that
    subclass made any later write of the same lead fail with
    "'DatetimeWithNanoseconds' object has no attribute '_nanosecond'".
    """
    from datetime import datetime, timezone

    from google.api_core.datetime_helpers import DatetimeWithNanoseconds

    value = DatetimeWithNanoseconds(2026, 8, 31, 20, 36, 8, 144338, tzinfo=timezone.utc)
    lead = _row_to_lead(make_row(last_sent_at=value, created_time=value))
    assert type(lead.last_sent_at) is datetime and type(lead.created_time) is datetime
    assert lead.last_sent_at == datetime(2026, 8, 31, 20, 36, 8, 144338)
    # and the lead can be turned into a Firestore document and encoded, as a save does
    from google.cloud.firestore_v1 import _helpers
    from app.services.firestore_stores import lead_to_document

    assert _helpers.encode_dict(lead_to_document(lead))
