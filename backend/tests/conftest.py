import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest


@pytest.fixture(autouse=True)
def _no_live_notification_llm(monkeypatch):
    """Tests must never reach the real OpenAI API -- the staff-notification
    email is stubbed by default; individual tests override it when they
    assert on its content."""
    from app.services import llm_service

    monkeypatch.setattr(
        llm_service, "generate_company_notification_email",
        lambda lead, result, text: llm_service.DraftOutput(subject="Notification", body_html="<p>stub</p>"),
    )


@pytest.fixture(autouse=True)
def _fresh_schedule_state(monkeypatch):
    """The follow-up schedule is cached module-level and persisted through a
    global state store -- reset both so tests never see each other's overrides."""
    from app.services import schedule_service, storage_service

    monkeypatch.setattr(storage_service, "_state_store", None)
    schedule_service.reset_cache()
    yield
    schedule_service.reset_cache()


@pytest.fixture(autouse=True)
def _fresh_draft_changes(monkeypatch):
    """Draft-change state is a module-level cached singleton -- never let one test's instructions leak into another."""
    from app.services import draft_changes_service, storage_service

    # An explicit in-memory store, so tests that flip use_mock_integrations off never reach for the real one.
    monkeypatch.setattr(storage_service, "_draft_change_store", storage_service.InMemoryDraftChangeStore())
    draft_changes_service.reset_cache()
    yield
    draft_changes_service.reset_cache()


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """The suite must pass on a fresh clone and whatever is in a developer's local .env: pin every
    setting the tests depend on to its default, and make sure no test can reach a real service with a
    real key (an empty OpenAI key makes any un-stubbed model call fail loudly instead of spending money)."""
    from app.config import settings

    for name, value in {
        "firestore_collection_prefix": "", "staff_email_allowlist": "", "environment": "development",
        "tavily_api_key": "", "use_mock_integrations": True, "openai_api_key": "",
        "company_notification_email": "", "gmail_sender_address": "",
        "company_name": "VRT Management Group, LLC",
        "company_address": "1 Botsford Hill Road, PO Box 150, Botsford, CT 06404",
        "public_base_url": "http://localhost:8000", "unsubscribe_secret": "test-secret",
        "daily_send_cap": 30, "follow_up_wait_days": 3, "max_follow_ups": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)
