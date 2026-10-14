"""The configuration check that runs when the API starts."""
import pytest

from app.config import Settings
from app.startup_checks import check_settings


def settings(**over) -> Settings:
    """A complete, valid production configuration, with overrides applied."""
    base = dict(
        _env_file=None, environment="production", use_mock_integrations=False, openai_api_key="sk-x",
        firebase_service_account_json='{"type":"service_account"}', firebase_project_id="proj",
        gmail_oauth_token_json='{"refresh_token":"r"}', gmail_sender_address="sales@vrt9.com",
        company_notification_email="staff@vrt9.com", public_base_url="https://api.example.com",
        unsubscribe_secret="a-long-random-value", allowed_origins="https://app.example.com",
        staff_email_allowlist="staff@vrt9.com",
        # never let a real file on the developer's machine satisfy a "missing" check
        gmail_oauth_token_file="/nonexistent/token.json", firebase_service_account_file="/nonexistent/key.json",
    )
    base.update(over)
    return Settings(**base)


def test_a_complete_production_configuration_has_no_errors_or_warnings():
    assert check_settings(settings()) == ([], [])


@pytest.mark.parametrize("field,expected", [
    ("openai_api_key", "OPENAI_API_KEY"),
    ("firebase_service_account_json", "FIREBASE_SERVICE_ACCOUNT"),
    ("firebase_project_id", "FIREBASE_PROJECT_ID"),
    ("gmail_oauth_token_json", "GMAIL_OAUTH_TOKEN"),
    ("gmail_sender_address", "GMAIL_SENDER_ADDRESS"),
    ("allowed_origins", "ALLOWED_ORIGINS"),
    ("staff_email_allowlist", "STAFF_EMAIL_ALLOWLIST"),
])
def test_each_missing_required_setting_is_named_in_an_error(field, expected):
    errors, _ = check_settings(settings(**{field: ""}))
    assert any(expected in e for e in errors), errors


def test_a_localhost_public_url_is_an_error_in_production_and_a_warning_in_development():
    prod_errors, _ = check_settings(settings(public_base_url="http://localhost:8000"))
    assert any("unsubscribe link" in e for e in prod_errors)
    dev_errors, dev_warnings = check_settings(settings(environment="development", public_base_url="http://localhost:8000"))
    assert not any("PUBLIC_BASE_URL" in e for e in dev_errors)
    assert any("PUBLIC_BASE_URL" in w for w in dev_warnings)


def test_a_missing_unsubscribe_secret_is_caught():
    errors, _ = check_settings(settings(unsubscribe_secret=""))
    assert any("UNSUBSCRIBE_SECRET" in e and "restart" in e for e in errors)


def test_a_missing_notification_email_is_only_a_warning():
    errors, warnings = check_settings(settings(company_notification_email=""))
    assert errors == [] and any("COMPANY_NOTIFICATION_EMAIL" in w for w in warnings)


def test_a_test_prefix_in_production_is_a_warning():
    _, warnings = check_settings(settings(firestore_collection_prefix="test_"))
    assert any("test collections" in w for w in warnings)


def test_mock_mode_is_an_error_in_production_and_fine_in_development():
    errors, _ = check_settings(settings(use_mock_integrations=True))
    assert any("USE_MOCK_INTEGRATIONS" in e for e in errors)
    assert check_settings(settings(use_mock_integrations=True, environment="development")) == ([], [])


def test_the_app_refuses_to_boot_in_production_with_an_incomplete_config(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings as live
    from app.main import app

    monkeypatch.setattr(live, "environment", "production")
    monkeypatch.setattr(live, "use_mock_integrations", False)
    monkeypatch.setattr(live, "openai_api_key", "")
    with pytest.raises(RuntimeError) as exc:
        with TestClient(app):
            pass
    assert "OPENAI_API_KEY" in str(exc.value) and "Refusing to start" in str(exc.value)


def test_the_app_still_boots_in_development_with_problems(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings as live
    from app.main import app

    monkeypatch.setattr(live, "auto_tick_interval_seconds", 0)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
