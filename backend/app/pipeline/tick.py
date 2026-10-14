"""
The per-cycle "tick": poll Gmail once, then run the outreach state machine
(outreach_graph) over every lead with something to check. Pulled out of the
route handler so it can be exercised directly in tests -- this is where the
reply-dedup logic lives, which needs its own coverage independent of the
HTTP layer.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.lead import AIResponseClassification, EmailStatus, Lead, QualifiedStatus, QualifiedStatusSource
from app.services import schedule_service
from app.pipeline.outreach_graph import record_reply, run_tick_for_lead
from app.pipeline.send_batch import BOUNCE_SENDER_MARKERS, process_inbound_for_bounce_or_unsubscribe
from app.services import llm_service
from app.services.gmail_service import GmailClientBase, InboundReply, begin_poll
from app.services.storage_service import LeadStoreBase, PipelineStateStore, hydrate_lead

logger = logging.getLogger(__name__)

# The auto-tick runs its blocking work in a worker thread (see
# scheduler.py's asyncio.to_thread), while a manual "Check replies" click
# runs synchronously on the event loop's own thread -- two genuinely
# different OS threads that can execute at the same moment. Without this
# lock, both could read the same Gmail history cursor, find the same
# "unclear" reply, and each independently send that lead the auto-generated
# clarification email -- a real, if narrow, duplicate-send window. A plain
# threading.Lock (not asyncio.Lock, which only guards a single thread/event
# loop) serializes the two: whichever arrives second simply waits for the
# first to finish, then runs cleanly against whatever cursor position that
# left behind. `with lock:` releases even if the tick raises, so a failure
# inside one tick can never leave the next one stuck waiting forever.
_tick_lock = threading.Lock()

# Small buffer so a reply landing within a few seconds of our own send
# (e.g. Gmail's own inbox copy of the message we just sent) never gets
# mistaken for a genuine inbound reply.
_DEDUP_BUFFER = timedelta(seconds=5)

# Once a reply has been classified interested/not_interested, that's a
# terminal outcome for the automated pipeline (§6.7: interested -> hands off
# to staff; not_interested -> outreach stops). last_sent_at never advances
# again for these leads, so the timestamp-based dedup below alone can't
# prevent the *same* old reply being classified (and the company
# re-notified) on every future tick -- this set is the actual stop condition.
_TERMINAL_CLASSIFICATIONS = (
    AIResponseClassification.interested,
    AIResponseClassification.not_interested,
)


@dataclass
class TickOutcome:
    lead_id: str
    action: str


def _as_naive_utc(dt):
    """
    Normalizes to a naive UTC datetime for comparison. The rest of the
    pipeline writes timestamps with datetime.utcnow() (naive), but a value
    round-tripped through storage comes back timezone-aware -- without this,
    comparing the two raises TypeError.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_genuinely_new(reply: InboundReply, last_sent_at) -> bool:
    """
    Guards against reprocessing: a poll can keep returning the same
    historical messages every tick, so only a reply that arrived after our
    most recent send to this lead counts as new.
    """
    if last_sent_at is None:
        return True
    received_naive = _as_naive_utc(reply.received_at)
    return received_naive > _as_naive_utc(last_sent_at) + _DEDUP_BUFFER


_STOPPED_STATUSES = (EmailStatus.bounced, EmailStatus.unsubscribed, EmailStatus.exhausted)


def _flag_late_reply(lead: Lead, reply: InboundReply, store: LeadStoreBase, gmail: GmailClientBase) -> None:
    """
    A lead can still reply after unsubscribing/bouncing/exhausting follow-ups
    -- stopping *outreach* to them doesn't mean their thread should go
    unwatched, or a real reply (a question, a change of mind) would just sit
    unseen. This surfaces the reply for staff -- the actual message text,
    plus the same AI read (classification + reasoning) an active lead's
    reply would get, so staff don't have to go dig up the raw thread just
    to know if it's worth a look. It must never trigger a draft or a send to
    the lead, or change email_status -- that's exactly the "no further
    outreach, ever" guarantee unsubscribe/stop is supposed to be.

    Written into `notes` (append-only) rather than a new field, with the
    message_id embedded as a dedup marker so the same reply isn't
    re-flagged every tick forever -- last_sent_at is frozen once a lead is
    stopped, so the usual timestamp-based "is this reply new" dedup alone
    can't tell "new since last tick" from "new since we stopped emailing".

    A genuinely "interested" late reply (e.g. someone who finally opens a
    follow-up after being exhausted) does get promoted the same way an
    active lead's reply would: ai_response_classification, a company
    notification, and -- only if the previous not_qualified was the
    pipeline's own auto-write, never a staff call -- qualified_status reset
    to unset so it surfaces in Needs Review instead of staying silently
    logged in notes where staff would only find it by opening the thread.
    """
    marker = f"[reply:{reply.message_id}]"
    if lead.notes and marker in lead.notes:
        return

    # Always saves below (at minimum, the notes append) -- must be hydrated
    # first, or that save would wipe this lead's real draft_history.
    hydrate_lead(lead, store)

    read = ""
    try:
        result = llm_service.classify_reply(lead, reply.body_text)
        read = f" AI read: {result.classification}."
    except Exception:  # noqa: BLE001 -- an LLM hiccup must not drop the flag itself
        result = None

    line = (
        f"⚠ Reply received after {lead.email_status.value} "
        f"({reply.received_at.strftime('%Y-%m-%d %H:%M UTC')}).{read} "
        f'Message: "{reply.body_text.strip()}" {marker}'
    )
    lead.notes = f"{lead.notes}\n{line}" if lead.notes else line

    record_reply(lead, reply)

    if result is not None:
        if result.classification == "interested":
            lead.ai_response_classification = AIResponseClassification.interested
            lead.requested_availability = result.requested_availability
            if lead.qualified_status_source == QualifiedStatusSource.ai:
                lead.qualified_status = QualifiedStatus.unset
                lead.qualified_status_source = None
                lead.qualified_status_reasoning = None

            notification = llm_service.generate_company_notification_email(lead, result, reply.body_text)
            if settings.company_notification_email:
                gmail.send(settings.company_notification_email, notification.subject, notification.body_html)
        elif result.classification == "not_interested":
            lead.ai_response_classification = AIResponseClassification.not_interested
        else:
            lead.ai_response_classification = AIResponseClassification.unclear

    store.save(lead)


def run_pipeline_tick(gmail: GmailClientBase, store: LeadStoreBase, state_store: PipelineStateStore) -> list[TickOutcome]:
    """Public entry point -- serialized so the auto-tick and a manual
    'Check replies' click can never actually run at the same time (see
    _tick_lock above)."""
    with _tick_lock:
        return _run_pipeline_tick_locked(gmail, store, state_store)


# How many ticks in a row may fail on the same new mail before the position in Gmail is saved anyway.
# A reply that can never be handled must not block every later reply forever -- but it is retried a few
# times first, because most failures (an OpenAI hiccup, a network error) are temporary.
_MAX_FAILED_TICKS = 5
_failed_ticks = 0


def _is_bounce_notice(msg: InboundReply) -> bool:
    return any(marker in msg.from_email.lower() for marker in BOUNCE_SENDER_MARKERS)


def _run_pipeline_tick_locked(gmail: GmailClientBase, store: LeadStoreBase, state_store: PipelineStateStore) -> list[TickOutcome]:
    """
    One cycle, shaped around Firestore's per-document billing: it touches
    only the leads that something actually happened to.

      1. New mail: one Gmail history call. Each new message is matched to its
         lead by thread (or, for mail clients that break threading, by sender
         address) -- one lookup per message, never a scan of every lead.
      2. Replies are handled per lead: bounce, late reply on a stopped lead,
         or classification for an active one.
      3. Timing: only the leads whose follow-up wait has passed
         (store.list_due_for_follow_up) -- a lead still inside its wait window
         is never read, so an idle tick costs about one read.

    The position in Gmail's history is saved only AFTER all of that succeeded.
    If anything failed (OpenAI down, a network error), it is left where it was,
    so the next tick sees the same mail again. That is safe because handling a
    reply is idempotent: a lead already classified interested or not interested
    is skipped, a handled "unclear" reply is no longer newer than the email we
    re-sent, a late reply is de-duplicated by its message id, and bounce notices
    are ignored once a lead is stopped. One lead failing never stops the others.

    Only leads with activity appear in the returned outcomes.
    """
    global _failed_ticks

    # Nothing has ever been emailed: nothing to watch, no point polling Gmail.
    if not store.has_contacted():
        return []

    # 1. New mail. The set of watched threads is only needed on the slow
    #    fallback path (first run, or an expired cursor), so it's built lazily.
    def watched_threads() -> set[str]:
        return {
            lead.thread_id
            for lead in store.list_contacted_light()
            if lead.thread_id
            and (lead.email_status in _STOPPED_STATUSES or lead.ai_response_classification not in _TERMINAL_CLASSIFICATIONS)
        }

    poll = begin_poll(gmail, state_store, watched_threads)

    results: list[TickOutcome] = []
    failures: list[str] = []
    handled: set[str] = set()
    replies_by_lead: dict[str, list[InboundReply]] = defaultdict(list)
    leads_by_id: dict[str, Lead] = {}
    by_thread: dict[str, Lead | None] = {}
    by_email: dict[str, Lead | None] = {}

    def lookup_thread(thread_id: str) -> Lead | None:
        if thread_id not in by_thread:
            by_thread[thread_id] = store.get_by_thread_id(thread_id)
        return by_thread[thread_id]

    def lookup_email(email: str) -> Lead | None:
        if email not in by_email:
            by_email[email] = store.find_by_email(email)
        return by_email[email]

    def fail(what: str, lead_id: str | None) -> None:
        logger.exception("Tick: %s failed; the Gmail position will not be saved, so it is retried next tick.", what)
        failures.append(what)
        if lead_id:
            results.append(TickOutcome(lead_id=lead_id, action="error"))

    def handle_message(msg: InboundReply) -> None:
        lead = lookup_thread(msg.thread_id)
        matched_by_thread = lead is not None
        if lead is None:
            # Some mail clients (confirmed live 2026-09-17, Yahoo Mail's webmail)
            # don't set In-Reply-To/References on a reply, so Gmail starts a
            # brand-new thread even though a human clearly replied to our email
            # -- a thread-only lookup would make that reply invisible forever.
            lead = lookup_email(msg.from_email.lower())
        if lead is None or not lead.thread_id or lead.email_status == EmailStatus.not_sent:
            return  # not mail from anyone we've emailed

        if lead.email_status in _STOPPED_STATUSES:
            if _is_bounce_notice(msg):
                return  # a delivery-failure notice isn't a reply (and was already acted on)
            # A reply on a stopped lead just gets flagged for staff and goes no
            # further -- never into classification/drafting.
            _flag_late_reply(lead, msg, store, gmail)
            results.append(TickOutcome(lead_id=lead.id, action="late_reply_flagged"))
            return
        if lead.ai_response_classification in _TERMINAL_CLASSIFICATIONS:
            return  # already at a terminal outcome; the old reply must not be reprocessed

        if not matched_by_thread:
            logger.info(
                "Reply from %s arrived on an unrecognized thread (%s) -- matched by sender "
                "address instead of thread_id, repointing the lead's tracked thread.",
                msg.from_email, msg.thread_id,
            )
            lead.thread_id = msg.thread_id

        # A bounce here saves the lead directly; leads fetched by thread/email are
        # complete (not light), so the save can't wipe real draft history.
        hydrate_lead(lead, store)
        outcome = process_inbound_for_bounce_or_unsubscribe(msg, lead, store)
        if outcome is not None:
            results.append(TickOutcome(lead_id=lead.id, action=outcome))
            handled.add(lead.id)
            return
        replies_by_lead[lead.id].append(msg)
        leads_by_id[lead.id] = lead

    for msg in poll.replies:
        try:
            handle_message(msg)
        except Exception:  # noqa: BLE001 -- one bad message must not stop the rest
            fail(f"handling message {msg.message_id}", None)

    # 2. Run the graph for each active lead with a genuinely new reply.
    for lead_id, replies in replies_by_lead.items():
        lead = leads_by_id[lead_id]
        if lead.email_status != EmailStatus.sent:
            continue
        new_replies = [r for r in replies if _is_genuinely_new(r, lead.last_sent_at)]
        if not new_replies:
            continue
        reply = max(new_replies, key=lambda r: r.received_at)
        try:
            action = run_tick_for_lead(lead, reply, gmail, store)
            results.append(TickOutcome(lead_id=lead.id, action=action))
            handled.add(lead.id)
        except Exception:  # noqa: BLE001
            fail(f"classifying the reply from {lead.email}", lead.id)

    # 3. Follow-ups that have come due (or leads to give up on after the last one).
    try:
        due = store.list_due_for_follow_up(datetime.utcnow())
    except Exception:  # noqa: BLE001
        fail("looking up due follow-ups", None)
        due = []
    for lead in due:
        if lead.id in handled or lead.email_status != EmailStatus.sent:
            continue
        try:
            action = run_tick_for_lead(lead, None, gmail, store)
            results.append(TickOutcome(lead_id=lead.id, action=action))
        except Exception:  # noqa: BLE001
            fail(f"follow-up for {lead.email}", lead.id)

    # Save the position only now that the mail it covers has been handled.
    if not failures:
        poll.commit()
        _failed_ticks = 0
    else:
        _failed_ticks += 1
        if _failed_ticks >= _MAX_FAILED_TICKS:
            logger.error(
                "Tick failed %d times in a row (%s). Saving the Gmail position anyway so one stuck message "
                "doesn't block every later reply; the affected lead(s) need a manual look.",
                _failed_ticks, "; ".join(failures),
            )
            poll.commit()
            _failed_ticks = 0
        else:
            logger.warning("Tick had %d failure(s); retrying the same mail next tick (%d/%d).",
                           len(failures), _failed_ticks, _MAX_FAILED_TICKS)
    return results
