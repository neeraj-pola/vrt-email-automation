"""
Regression coverage for a real bug: GET /public/unsubscribe used to
unsubscribe the lead immediately, on the bare GET. Mail clients and
security gateways (Gmail, Outlook Safe Links, corporate scanners)
routinely pre-fetch every link in an email to check it's safe -- which
silently "clicked" that link before any human ever saw the message. Every
lead in a real batch got unsubscribed within seconds of being sent, before
anyone could have clicked anything.

Fix: GET only renders a confirmation page and must not mutate anything;
the actual unsubscribe only happens on a real POST (submitted by a button
press, not something a passive link-scanner does).
"""
from datetime import datetime
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.api.deps import get_store
from app.main import app
from app.models.lead import EmailStatus, Lead
from app.services.storage_service import InMemoryLeadStore
from app.services.unsubscribe import build_unsubscribe_url


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
        email_status=EmailStatus.sent,
    )
    defaults.update(overrides)
    return Lead(**defaults)


def make_client(lead: Lead) -> TestClient:
    store = InMemoryLeadStore()
    store.upsert_many([lead])
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def test_get_unsubscribe_link_does_not_unsubscribe():
    lead = make_lead()
    client = make_client(lead)
    try:
        url = build_unsubscribe_url(lead.id)
        split = urlsplit(url)
        resp = client.get(f"{split.path}?{split.query}")
        assert resp.status_code == 200
        assert "confirm" in resp.text.lower()

        store: InMemoryLeadStore = app.dependency_overrides[get_store]()
        assert store.get(lead.id).email_status == EmailStatus.sent
    finally:
        app.dependency_overrides.clear()


def test_post_unsubscribe_actually_unsubscribes():
    lead = make_lead()
    client = make_client(lead)
    try:
        url = build_unsubscribe_url(lead.id)
        token = url.split("token=")[1]
        resp = client.post("/public/unsubscribe", data={"lead_id": lead.id, "token": token})
        assert resp.status_code == 200
        assert "unsubscribed" in resp.text.lower()

        store: InMemoryLeadStore = app.dependency_overrides[get_store]()
        assert store.get(lead.id).email_status == EmailStatus.unsubscribed
    finally:
        app.dependency_overrides.clear()


def test_get_on_already_unsubscribed_lead_shows_already_page_without_erroring():
    lead = make_lead(email_status=EmailStatus.unsubscribed)
    client = make_client(lead)
    try:
        url = build_unsubscribe_url(lead.id)
        split = urlsplit(url)
        resp = client.get(f"{split.path}?{split.query}")
        assert resp.status_code == 200
        assert "already" in resp.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_invalid_token_rejected_on_get():
    lead = make_lead()
    client = make_client(lead)
    try:
        resp = client.get(f"/public/unsubscribe?lead_id={lead.id}&token=garbage")
        assert resp.status_code == 400

        store: InMemoryLeadStore = app.dependency_overrides[get_store]()
        assert store.get(lead.id).email_status == EmailStatus.sent
    finally:
        app.dependency_overrides.clear()


def test_click_logs_engagement_and_redirects_to_the_real_site():
    lead = make_lead()
    client = make_client(lead)
    try:
        resp = client.get(f"/public/click?lead_id={lead.id}&target=website", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://www.vrtvega.com/"

        store: InMemoryLeadStore = app.dependency_overrides[get_store]()
        assert store.get(lead.id).website_clicked_at is not None
    finally:
        app.dependency_overrides.clear()


def test_click_get_started_target_redirects_correctly():
    lead = make_lead()
    client = make_client(lead)
    try:
        resp = client.get(f"/public/click?lead_id={lead.id}&target=get_started", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://www.vrtvega.com/talk-to-rajesh"

        store: InMemoryLeadStore = app.dependency_overrides[get_store]()
        updated = store.get(lead.id)
        assert updated.get_started_clicked_at is not None
        assert updated.website_clicked_at is None  # the other field untouched
    finally:
        app.dependency_overrides.clear()


def test_click_with_unknown_target_rejected():
    lead = make_lead()
    client = make_client(lead)
    try:
        resp = client.get(f"/public/click?lead_id={lead.id}&target=not_a_real_target")
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_click_with_unknown_lead_id_still_redirects():
    """
    A tracking hiccup (or a forged/stale lead_id) must never block a real
    person from reaching the site they clicked through to -- only skip the
    logging, never show an error page instead of the redirect.
    """
    lead = make_lead()
    client = make_client(lead)
    try:
        resp = client.get("/public/click?lead_id=does-not-exist&target=website", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://www.vrtvega.com/"
    finally:
        app.dependency_overrides.clear()
