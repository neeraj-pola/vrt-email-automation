"""
Checks the configuration when the API starts, so a bad deploy fails at boot with a plain list of what is
missing -- not on the first request, or (worse) silently: unsubscribe links that stop working after a
restart, replies that never get read, a dashboard the browser can't reach.

Outside development, any ERROR stops the app from starting. In development everything is only logged.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.config import Settings


def check_settings(s: Settings) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings) -- each a sentence saying what is wrong and what to set."""
    errors: list[str] = []
    warnings: list[str] = []
    production = s.environment != "development"

    if s.use_mock_integrations:
        if production:
            errors.append(
                "USE_MOCK_INTEGRATIONS=true outside development: the app would use fake Gmail and in-memory "
                "storage and send nothing. Set USE_MOCK_INTEGRATIONS=false."
            )
        return errors, warnings  # nothing else matters in mock mode

    def missing(name: str, ok: bool, why: str) -> None:
        if not ok:
            errors.append(f"{name} is not set. {why}")

    missing("OPENAI_API_KEY", bool(s.openai_api_key), "Drafting and reply classification need it.")

    has_firebase = bool(
        s.firebase_service_account_json
        or (s.firebase_service_account_file and Path(s.firebase_service_account_file).exists())
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    missing("FIREBASE_SERVICE_ACCOUNT_JSON (or _FILE)", has_firebase,
            "Firestore and staff login need the Firebase service-account key.")
    missing("FIREBASE_PROJECT_ID", bool(s.firebase_project_id), "Needed to verify staff logins.")

    has_gmail = bool(s.gmail_oauth_token_json or (s.gmail_oauth_token_file and Path(s.gmail_oauth_token_file).exists()))
    missing("GMAIL_OAUTH_TOKEN_JSON (or a token file)", has_gmail,
            "Run `python -m app.authorize_gmail` once locally, then paste the token file's contents here.")
    missing("GMAIL_SENDER_ADDRESS", bool(s.gmail_sender_address), "It must be the Gmail account the token belongs to.")

    if not s.company_notification_email:
        warnings.append("COMPANY_NOTIFICATION_EMAIL is not set: staff will NOT be emailed when a lead replies as interested.")

    local_url = s.public_base_url.startswith(("http://localhost", "http://127.0.0.1"))
    if local_url:
        (errors if production else warnings).append(
            "PUBLIC_BASE_URL points at localhost, so the unsubscribe link in every email only works on this machine. "
            "Set it to this API's public https address."
        )
    elif production and not s.public_base_url.startswith("https://"):
        warnings.append("PUBLIC_BASE_URL is not https. Unsubscribe and click links should be https.")

    if s.unsubscribe_secret_generated:
        (errors if production else warnings).append(
            "UNSUBSCRIBE_SECRET is not set, so a random one is generated at every start and every unsubscribe link "
            "already sent stops working after a restart. Set a long random value (see the README)."
        )

    if production:
        missing("ALLOWED_ORIGINS", bool(s.allowed_origins.strip()),
                "The deployed frontend's address; without it the browser blocks every request.")
        missing("STAFF_EMAIL_ALLOWLIST", bool(s.staff_email_allowlist.strip()),
                "Comma-separated staff emails allowed into the dashboard; without it everyone is refused.")
        if s.firestore_collection_prefix:
            warnings.append(f"FIRESTORE_COLLECTION_PREFIX={s.firestore_collection_prefix!r} is set: this deployment uses the "
                            "test collections, not the real ones. Leave it empty for real data.")
    return errors, warnings
