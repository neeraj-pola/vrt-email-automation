"""Staff login: a Firebase ID token is verified, and the email must be on the staff list."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import firebase_app, storage_service


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "use_mock_integrations", False)
    monkeypatch.setattr(storage_service, "_store", storage_service.InMemoryLeadStore())  # no Firestore needed for /leads


def token_for(monkeypatch, email="staff@vrt9.com", uid="u1"):
    monkeypatch.setattr(firebase_app, "verify_staff_token", lambda t: {"email": email, "uid": uid} if t == "good" else (_ for _ in ()).throw(ValueError("bad token")))


def get(headers=None):
    return TestClient(app).get("/leads", headers=headers or {})


def test_no_token_is_refused(live):
    assert get().status_code == 401


def test_a_bad_token_is_refused(live, monkeypatch):
    token_for(monkeypatch)
    r = get({"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and "Invalid staff token" in r.json()["detail"]


def test_a_listed_staff_member_gets_in_case_insensitively(live, monkeypatch):
    token_for(monkeypatch, email="Staff@VRT9.com")
    monkeypatch.setattr(settings, "staff_email_allowlist", "staff@vrt9.com, other@vrt9.com")
    assert get({"Authorization": "Bearer good"}).status_code == 200


def test_a_valid_login_that_is_not_on_the_list_is_refused(live, monkeypatch):
    token_for(monkeypatch, email="stranger@example.com")
    monkeypatch.setattr(settings, "staff_email_allowlist", "staff@vrt9.com")
    r = get({"Authorization": "Bearer good"})
    assert r.status_code == 403 and "not on the staff list" in r.json()["detail"]


def test_outside_development_an_empty_list_refuses_everyone(live, monkeypatch):
    token_for(monkeypatch)
    monkeypatch.setattr(settings, "staff_email_allowlist", "")
    monkeypatch.setattr(settings, "environment", "production")
    r = get({"Authorization": "Bearer good"})
    assert r.status_code == 403 and "STAFF_EMAIL_ALLOWLIST" in r.json()["detail"]


def test_in_development_an_empty_list_lets_any_valid_login_in(live, monkeypatch):
    token_for(monkeypatch)
    monkeypatch.setattr(settings, "staff_email_allowlist", "")
    monkeypatch.setattr(settings, "environment", "development")
    assert get({"Authorization": "Bearer good"}).status_code == 200


def test_mock_mode_needs_no_login():
    assert get().status_code == 200
