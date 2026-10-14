"""
Renders the actual HTML shell every outbound email goes out in: a plain text
header, the drafted content, a fixed line pointing to the company site, and
a footer with the required CAN-SPAM elements (§8) -- including a REAL
working unsubscribe link, not the placeholder string that used to sit in
the footer.

The draft content (subject + a few paragraphs of body copy, AI-written for
follow-ups, a fixed template for the initial email) is never responsible
for the shell; this module is what turns it into a complete, on-brand
email. Keeping the shell out of the draft means the logo/footer/
unsubscribe link/site link can't be dropped, reworded, or malformed.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from app.config import settings
from app.models.lead import Lead
from app.services.click_tracking import CLICK_TARGETS, build_click_url
from app.services.unsubscribe import build_unsubscribe_url

# Inlined as base64 rather than fetched from a URL -- an <img src="...">
# pointing at our own server only works once that server has a real,
# publicly-reachable address. Locally that's PUBLIC_BASE_URL=http://
# localhost:8000, which means "this machine" to whoever's looking at it --
# Gmail's servers (or the recipient's) have no way to reach a sender's own
# laptop, so the image just shows as broken. Embedding the bytes directly
# means the logo renders regardless of what's running, local or deployed.
# Pre-resized to 84x84 (backend/app/static/logo_small.png) before encoding --
# the source logo is a 205KB 2295x2295 PNG, displayed at 28px; inlining that
# untouched would triple-plus the size of every single outbound email.
_LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "logo_small.png"
_LOGO_DATA_URI = (
    "data:image/png;base64," + base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    if _LOGO_PATH.exists()
    else ""
)

# Replaces the old per-lead "featured project" showcase (matched by ad
# campaign category) -- staff asked for a plain, fixed mention of the real
# site instead, rather than an emailed guess at which portfolio piece a
# lead might like. Per-lead now (not a plain constant) since both links are
# routed through /public/click to log a real click as an engagement signal
# (see app/services/click_tracking.py) before redirecting to the real page.
def _site_links_html(lead_id: str, track: bool = True) -> str:
    # track=False (staff preview, not a real send) points straight at the
    # real URLs, skipping /public/click entirely -- otherwise a staff
    # member just looking at a draft could click "Get started" out of
    # curiosity and it would falsely log as the *lead's* engagement. Same
    # class of bug already fixed once for the unsubscribe link.
    designs_url = build_click_url(lead_id, "website") if track else CLICK_TARGETS["website"]
    get_started_url = build_click_url(lead_id, "get_started") if track else CLICK_TARGETS["get_started"]
    return f"""
<p style="margin:22px 0 0;font-size:14px;line-height:1.6;color:#615a55;">
  Learn more about our programs at
  <a href="{designs_url}" style="color:#cc1b1b;">vrtvega.com</a>.
</p>
<p style="margin:6px 0 0;font-size:14px;line-height:1.6;color:#615a55;">
  Book a strategy call: <a href="{get_started_url}" style="color:#cc1b1b;">vrtvega.com/talk-to-rajesh</a>
</p>
""".strip()


def render_email_html(lead: Lead, subject: str, inner_body_html: str,
                       include_unsubscribe: bool = True,
                       interactive_unsubscribe: bool = True) -> str:
    """
    Wraps the drafted inner content in the full branded email shell.

    interactive_unsubscribe=False renders the footer looking identical but
    with an inert, non-navigating "Unsubscribe" -- used for the staff-facing
    preview (routes_drafts.preview_draft). That preview is a real signed
    link rendered in an iframe; without this, a staff member just looking at
    what an email will say could click it and actually unsubscribe the lead
    before anything was ever sent. Only a real outbound send (§8) gets the
    working link.
    """
    unsubscribe_html = ""
    if include_unsubscribe:
        if interactive_unsubscribe:
            unsubscribe_url = build_unsubscribe_url(lead.id)
            unsubscribe_html = f'<a href="{unsubscribe_url}" style="color:#cc1b1b;">Unsubscribe</a>'
        else:
            unsubscribe_html = (
                '<span style="color:#cc1b1b;text-decoration:underline;" '
                'title="Preview only -- inactive here, real in the sent email">Unsubscribe</span>'
            )

    # Kept deliberately close to a plain note: no full-width solid color
    # banner, no card shadow/border-radius chrome -- those visual cues read
    # as "marketing template" to both a human eye and a content-based spam
    # filter. Just the real logo, then a quiet max-width column --
    # everything actually needed for this to render cleanly across clients.
    logo_img = (
        f'<img src="{_LOGO_DATA_URI}" width="120" alt="VRT Management Group" '
        'style="display:block;height:auto;border:0;">'
        if _LOGO_DATA_URI
        else '<span style="font-family:Georgia,serif;font-size:17px;color:#16171c;">VRT Management Group</span>'
    )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:24px 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">

      <tr><td style="padding-bottom:18px;border-bottom:1px solid #e3d6cd;">
        {logo_img}
      </td></tr>

      <tr><td style="padding:24px 0 4px;">
        <div style="font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:15px;line-height:1.65;color:#16171c;">
          {inner_body_html}
        </div>
        {_site_links_html(lead.id, track=interactive_unsubscribe)}
      </td></tr>

      <tr><td style="padding:20px 0 4px;border-top:1px solid #efe3da;">
        <p style="margin:16px 0 4px;font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12px;color:#7d7570;">
          {settings.company_name} &middot; {settings.company_address}
        </p>
        <p style="margin:0;font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12px;color:#7d7570;">
          Don't want to hear from us again? {unsubscribe_html}
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
""".strip()


def strip_html_shell_if_present(body_html: str) -> str:
    """
    Defensive helper: if a draft was generated before this template existed
    (or an LLM ignores instructions and returns a full shell anyway), unwrap
    to just the inner content so we don't double-wrap on the next render.
    """
    match = re.search(r"<body[^>]*>(.*)</body>", body_html, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else body_html
