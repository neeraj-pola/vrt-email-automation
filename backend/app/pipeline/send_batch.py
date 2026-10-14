"""
Rate-limited batch sending (§8) + bounce/unsubscribe handling (§5 stage 5, §8).

Sending is capped per day rather than firing all leads at once, to protect
deliverability -- this matters more on a personal Gmail account than a
warmed business domain (§8).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.models.lead import EmailStatus, Lead
from app.pipeline.outreach_graph import send_initial_outreach, stop_outreach_permanently
from app.services.gmail_service import GmailClientBase, InboundReply
from app.services.storage_service import LeadStoreBase

BOUNCE_SENDER_MARKERS = ("mailer-daemon", "postmaster")

# Company is in Botsford, CT (§2) -- "today" for the daily send cap and
# the dashboard's live stat has to mean the business's actual calendar day,
# not UTC's. Using naive UTC midnight as the cutoff meant the day rolled
# over 4-5 hours early (EDT/EST) from the company's perspective: everything
# sent earlier in a real business day got bucketed into "yesterday" the
# moment UTC ticked past midnight, while local evening hadn't even reached
# it yet. Caught 2026-09-13: 149 real sends that day, only 18 showing as
# "sent today" less than an hour after UTC's date changed.
_COMPANY_TZ = ZoneInfo("America/New_York")


def leads_sent_today(store: LeadStoreBase, now: datetime | None = None) -> int:
    # now is injectable for tests. Naive input is treated as UTC, matching
    # this codebase's convention everywhere else (lead.last_sent_at itself
    # is stored naive-UTC -- see tick.py's _as_naive_utc).
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_local = now.astimezone(_COMPANY_TZ)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # lead.last_sent_at is stored as naive UTC throughout this codebase (see
    # tick.py's _as_naive_utc) -- convert the local cutoff to match.
    today_start_utc = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    return store.count_sent_since(today_start_utc)  # a count query, not a read of every lead


def send_approved_batch(lead_ids: list[str], gmail: GmailClientBase, store: LeadStoreBase) -> dict:
    """
    Sends initial outreach for the given leads, stopping at the daily cap
    (§8). Leads beyond the cap are left un-sent (still `not_sent`) for the
    next day's run -- nothing is silently dropped.
    """
    already_sent = leads_sent_today(store)
    remaining_capacity = max(0, settings.daily_send_cap - already_sent)

    sent, deferred, errors = [], [], []
    attempts = 0
    for lead_id in lead_ids:
        if len(sent) >= remaining_capacity:
            deferred.append(lead_id)
            continue
        lead = store.get(lead_id)
        if lead is None:
            errors.append({"lead_id": lead_id, "error": "not found"})
            continue
        if lead.draft_locked:
            # Covers bounced/unsubscribed/exhausted, but also -- and this is
            # the real gap this used to leave open -- classified interested
            # or not_interested. Those leave email_status untouched (still
            # "sent") and don't clear current_draft_subject/body_html, only
            # archive it to history, so the old enumerated-status check
            # would have let a bulk "select all -> send" actually re-send
            # that stale locked draft to a lead who should get no further
            # outreach at all. draft_locked is the one field every one of
            # these terminal cases already sets True, so it's the correct
            # single source of truth for "nothing should go out right now."
            errors.append({
                "lead_id": lead_id,
                "error": f"blocked: no pending draft (status={lead.email_status}, "
                         f"classification={lead.ai_response_classification})",
            })
            continue
        # A small proactive gap between actual Gmail calls, same fix as
        # poll_new_replies got earlier -- reply-polling and bulk sending
        # share the same per-minute Gmail quota, and firing every send
        # back-to-back with zero spacing is exactly what bursts past it in
        # the first place, not just something to recover from afterward via
        # retry. Only counts real attempts, not leads skipped above (a
        # deferred/blocked lead never touches Gmail, so it shouldn't cost a
        # throttle delay).
        if attempts > 0:
            time.sleep(0.15)
        attempts += 1
        try:
            send_initial_outreach(lead, gmail, store)
            sent.append(lead_id)
        except Exception as e:  # noqa: BLE001
            errors.append({"lead_id": lead_id, "error": str(e)})

    return {"sent": sent, "deferred_to_next_batch": deferred, "errors": errors,
            "daily_cap": settings.daily_send_cap, "already_sent_today": already_sent}


def process_inbound_for_bounce_or_unsubscribe(reply: InboundReply, lead: Lead, store: LeadStoreBase) -> str | None:
    """
    Checks one inbound message for a bounce signal before it reaches normal
    reply classification. Returns the action taken, or None if this message
    is a normal reply that should go through classification.

    Unsubscribing is deliberately NOT detected from reply text here anymore
    -- it used to scan the body for keywords like "unsubscribe", but every
    outbound email's own footer contains that exact word ("Unsubscribe"),
    and quote-stripping a real reply is inherently unreliable across mail
    clients (Gmail's quoted-text formatting varies enough that the footer
    routinely survives into body_text). That meant almost *any* reply could
    false-positive as an unsubscribe request -- a real production bug: every
    lead in a batch got auto-unsubscribed the first time any of them simply
    replied. The signed link (§8, routes_public.py) is now the only way a
    lead actually unsubscribes; a reply that says "please stop emailing me"
    instead goes through normal classification, which reads as
    not_interested and stops outreach through the ordinary path anyway.

    Takes the lead directly (the caller -- tick.py -- already has it from a
    single store.list_all_light() and a thread_id map) rather than looking it
    up here: this used to call store.get_by_thread_id() once per inbound
    message, which meant a database round-trip for every piece of mail in
    the inbox the poll saw, whether or not it had anything to do with a
    lead. The caller hydrates the lead (storage_service.hydrate_lead) before
    calling this, since a bounce match here saves it directly.
    """
    sender = reply.from_email.lower()
    if any(marker in sender for marker in BOUNCE_SENDER_MARKERS):
        stop_outreach_permanently(lead, EmailStatus.bounced)
        store.save(lead)
        return "bounced"

    return None
