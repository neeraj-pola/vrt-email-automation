"""
Real, working unsubscribe mechanism (CLAUDE.md §8): every outreach email
must include one, and clicking it must immediately and permanently stop all
further outreach. Previously the footer just contained the literal string
"{unsubscribe_link}" -- not a real link. This signs a token per lead so the
link works with no login required (an email link can't ask someone to sign
in first) while still not being guessable from the lead_id alone.
"""
from __future__ import annotations

import hashlib
import hmac

from app.config import settings


def _sign(lead_id: str) -> str:
    return hmac.new(settings.unsubscribe_secret.encode(), lead_id.encode(), hashlib.sha256).hexdigest()[:32]


def build_unsubscribe_url(lead_id: str) -> str:
    token = _sign(lead_id)
    return f"{settings.public_base_url}/public/unsubscribe?lead_id={lead_id}&token={token}"


def verify_unsubscribe_token(lead_id: str, token: str) -> bool:
    expected = _sign(lead_id)
    return hmac.compare_digest(expected, token)
