"""
The unsubscribe link used to be the literal string "{unsubscribe_link}" --
never a real URL, so it was unclickable and had no backing endpoint. These
cover the fix: a signed, verifiable, per-lead link.
"""
from app.services.unsubscribe import build_unsubscribe_url, verify_unsubscribe_token


def test_built_url_is_verifiable():
    url = build_unsubscribe_url("lead-123")
    assert url.startswith("http")
    assert "lead_id=lead-123" in url
    token = url.split("token=")[1]
    assert verify_unsubscribe_token("lead-123", token) is True


def test_token_does_not_verify_for_a_different_lead():
    url = build_unsubscribe_url("lead-123")
    token = url.split("token=")[1]
    assert verify_unsubscribe_token("lead-456", token) is False


def test_garbage_token_rejected():
    assert verify_unsubscribe_token("lead-123", "not-a-real-token") is False
