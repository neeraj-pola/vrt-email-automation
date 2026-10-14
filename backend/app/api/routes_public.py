"""
Public, unauthenticated routes -- reached from links inside outbound emails,
not from the staff dashboard. No require_staff dependency on anything here.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.deps import get_store
from app.models.lead import EmailStatus
from app.pipeline.outreach_graph import stop_outreach_permanently
from app.services.click_tracking import CLICK_TARGETS
from app.services.storage_service import LeadStoreBase
from app.services.unsubscribe import verify_unsubscribe_token

router = APIRouter(prefix="/public", tags=["public"])


def _page(title: str, message: str, form: str = "") -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background:#f7f5f3; color:#16171c;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:24px; }}
  .card {{ max-width:420px; text-align:center; background:#ffffff; border:1px solid #e3d6cd;
           border-radius:14px; padding:36px 30px; }}
  h1 {{ font-size:20px; margin:0 0 12px; }}
  p {{ color:#615a55; font-size:14.5px; line-height:1.6; margin:0 0 20px; }}
  button {{ font-family:inherit; font-size:15px; font-weight:600; padding:12px 22px; border-radius:9px;
            border:none; background:#cc1b1b; color:#ffffff; cursor:pointer; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p>{form}</div></body></html>"""


@router.get("/unsubscribe", response_class=HTMLResponse)
async def confirm_unsubscribe(lead_id: str, token: str, store: LeadStoreBase = Depends(get_store)):
    """
    Shows a confirmation page only -- deliberately does NOT unsubscribe on
    GET. Mail clients and security gateways (Gmail, Outlook Safe Links,
    corporate scanners) routinely pre-fetch every link in an email to check
    it's safe, which silently "clicks" a GET-triggers-the-action link before
    any human ever sees the message. That's a real bug this had: every lead
    in a batch got unsubscribed within seconds of being sent, before anyone
    could have clicked anything. The actual unsubscribe only happens from a
    real button press, which POSTs below -- something a passive link
    scanner doesn't do.
    """
    if not verify_unsubscribe_token(lead_id, token):
        return HTMLResponse(_page("Link not recognized", "This unsubscribe link is invalid or expired."), status_code=400)

    lead = store.get(lead_id)
    if lead is None:
        return HTMLResponse(_page("Not found", "We couldn't find this subscription."), status_code=404)

    if lead.email_status == EmailStatus.unsubscribed:
        return HTMLResponse(_page(
            "Already unsubscribed",
            f"{lead.full_name}, you're already unsubscribed -- no further emails will be sent.",
        ))

    form = f"""<form method="post" action="/public/unsubscribe">
      <input type="hidden" name="lead_id" value="{lead_id}">
      <input type="hidden" name="token" value="{token}">
      <button type="submit">Yes, unsubscribe me</button>
    </form>"""
    return HTMLResponse(_page(
        "Unsubscribe from VRT Management Group, LLC?",
        f"{lead.full_name}, click below to confirm. You won't receive any further emails from us.",
        form,
    ))


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(lead_id: str = Form(...), token: str = Form(...), store: LeadStoreBase = Depends(get_store)):
    if not verify_unsubscribe_token(lead_id, token):
        return HTMLResponse(_page("Link not recognized", "This unsubscribe link is invalid or expired."), status_code=400)

    lead = store.get(lead_id)
    if lead is None:
        return HTMLResponse(_page("Not found", "We couldn't find this subscription."), status_code=404)

    # Idempotent and permanent (§8): stops all further outreach, no matter
    # how many times this is submitted.
    stop_outreach_permanently(lead, EmailStatus.unsubscribed)
    store.save(lead)

    return HTMLResponse(_page(
        "You're unsubscribed",
        f"{lead.full_name}, you won't receive any further emails from VRT Management Group, LLC. "
        f"If this was a mistake, just reply to any earlier email and let us know.",
    ))


@router.get("/click")
async def track_click(lead_id: str, target: str, store: LeadStoreBase = Depends(get_store)):
    """
    Logs a real click (a trustworthy engagement signal, see
    app/services/click_tracking.py) then redirects to the actual
    destination. Always redirects even if lead_id doesn't resolve to a real
    lead or logging fails -- a person clicking a link in their email wants
    to reach the site; a tracking hiccup on our end must never block that
    or show them an error page instead.
    """
    dest = CLICK_TARGETS.get(target)
    if dest is None:
        return HTMLResponse(_page("Link not recognized", "This link is invalid or expired."), status_code=400)

    lead = store.get(lead_id)
    if lead is not None:
        if target == "website":
            lead.website_clicked_at = datetime.utcnow()
        elif target == "get_started":
            lead.get_started_clicked_at = datetime.utcnow()
        store.save(lead)

    return RedirectResponse(url=dest, status_code=302)
