"""Shared FastAPI dependencies: service singletons + staff auth (Firebase)."""
from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings
from app.services.gmail_service import GmailClientBase, get_gmail_client
from app.services.storage_service import LeadStoreBase, PipelineStateStore, get_lead_store, get_pipeline_state_store


def get_store() -> LeadStoreBase:
    return get_lead_store()


def get_gmail() -> GmailClientBase:
    return get_gmail_client()


def get_pipeline_state() -> PipelineStateStore:
    return get_pipeline_state_store()


def require_staff(authorization: str | None = Header(default=None)) -> str:
    """
    Employee login (§9): the frontend signs staff in with Firebase Auth and
    forwards the resulting ID token as a Bearer token; it is verified here.

    A valid Firebase login alone is not enough -- anyone holding the public
    web API key can try to create an account -- so the email must also be on
    STAFF_EMAIL_ALLOWLIST. Outside development that list is mandatory: with
    it unset every request is refused rather than silently open.

    In mock-integration mode (local/synthetic testing, §13.1) auth is not
    enforced, so the pipeline can be exercised without a Firebase project.
    """
    if settings.use_mock_integrations:
        return "mock-staff-user"

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing staff authorization token")

    from app.services import firebase_app

    token = authorization.split(" ", 1)[1]
    try:
        claims = firebase_app.verify_staff_token(token)
    except Exception as e:  # noqa: BLE001 -- invalid, expired or revoked token
        raise HTTPException(status_code=401, detail=f"Invalid staff token: {e}") from e

    email = (claims.get("email") or "").lower()
    allowed = firebase_app.staff_allowlist()
    if allowed is None:
        if settings.environment != "development":
            raise HTTPException(status_code=403, detail="STAFF_EMAIL_ALLOWLIST is not configured on the server.")
    elif email not in allowed:
        raise HTTPException(status_code=403, detail="This account is not on the staff list.")
    return claims.get("uid") or email
