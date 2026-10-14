"""
Click tracking for links inside outbound emails. A real click is a
trustworthy engagement signal in a way an open-pixel "opened" signal isn't
-- Apple Mail's Privacy Protection pre-fetches every image in every email
regardless of whether a human ever actually opens it, so an open pixel
would just show ~100% "opened" for any Apple Mail lead and mean nothing.
A click requires an actual deliberate tap.

No signed token here (unlike unsubscribe, see app/services/unsubscribe.py)
-- the worst case of someone guessing/forging a lead_id is a false
"clicked" timestamp showing on the dashboard, not a destructive action, so
a plain UUID lookup is proportionate to the stakes.

target is always a fixed key from CLICK_TARGETS, never an arbitrary URL --
deliberately, so this can never be turned into an open redirect off our
own domain (?dest=https://anything would let anyone use our domain to mask
a link to somewhere else entirely).
"""
from __future__ import annotations

from app.config import settings

CLICK_TARGETS: dict[str, str] = {
    "website": "https://www.vrtvega.com/",
    "get_started": "https://www.vrtvega.com/talk-to-rajesh",
}


def build_click_url(lead_id: str, target: str) -> str:
    if target not in CLICK_TARGETS:
        raise ValueError(f"Unknown click target: {target!r}. Must be one of {list(CLICK_TARGETS)}.")
    return f"{settings.public_base_url}/public/click?lead_id={lead_id}&target={target}"
