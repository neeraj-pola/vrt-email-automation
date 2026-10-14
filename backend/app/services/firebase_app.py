"""One shared firebase-admin app: Firestore for data, Firebase Auth for staff login."""
from __future__ import annotations

import json
import threading
from typing import Optional

from app.config import settings

_lock = threading.Lock()
_app = None


def get_app():
    global _app
    with _lock:
        if _app is None:
            import firebase_admin
            from firebase_admin import credentials

            if settings.firebase_service_account_json:
                cred = credentials.Certificate(json.loads(settings.firebase_service_account_json))
            elif settings.firebase_service_account_file:
                cred = credentials.Certificate(settings.firebase_service_account_file)
            else:
                cred = credentials.ApplicationDefault()  # e.g. GOOGLE_APPLICATION_CREDENTIALS
            options = {"projectId": settings.firebase_project_id} if settings.firebase_project_id else None
            _app = firebase_admin.initialize_app(cred, options)
        return _app


_db = None


def get_firestore_client():
    global _db
    with _lock:
        have = _db is not None
    if not have:
        from firebase_admin import firestore

        client = firestore.client(get_app())
        with _lock:
            _db = client
    return _db


def verify_staff_token(token: str) -> dict:
    """Verifies a Firebase ID token and returns its claims (raises on an invalid or expired one)."""
    from firebase_admin import auth

    return auth.verify_id_token(token, app=get_app())


def staff_allowlist() -> Optional[set[str]]:
    emails = {e.strip().lower() for e in settings.staff_email_allowlist.split(",") if e.strip()}
    return emails or None
