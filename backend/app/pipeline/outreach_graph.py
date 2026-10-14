"""
Stages 4-8 (CLAUDE.md §6): send & track, bounce handling, reply detection/
classification, qualification hand-off, and the follow-up loop -- built as an
explicit LangGraph state machine per lead, rather than one large prompt.

The graph is deterministic where possible (status checks, timing, counters)
and only calls the LLM where judgment is genuinely needed (classification,
qualification). It's invoked as a per-lead "tick": run once per lead each
time the scheduler polls (see app/api/routes_pipeline.py), and each run is
idempotent -- a lead with nothing new to do simply passes through untouched.

Draft-ahead model: whenever a stage's email actually goes out (the initial
send, or any follow-up), the pipeline immediately pre-generates the NEXT
stage's draft and leaves it unlocked in current_draft_* -- so staff always
see what's coming next, can edit it, and can send it early (same
"Approve & send" flow as the very first email) instead of only finding out
once it fires automatically at next_follow_up_due_at. If a reply arrives
before that pre-drafted follow-up goes out, it's preserved in draft_history
marked unsent (see DraftVersion.sent) rather than silently discarded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

from langgraph.graph import StateGraph, END

from app.config import settings
from app.models.lead import (
    AIResponseClassification,
    DraftVersion,
    DraftVersionSource,
    EmailStatus,
    Lead,
    QualifiedStatus,
    QualifiedStatusSource,
)
from app.services import llm_service, schedule_service
from app.services.email_template import render_email_html
from app.services.gmail_service import GmailClientBase, InboundReply
from app.services.storage_service import LeadStoreBase, hydrate_lead


def _stage_label(follow_up_count: int) -> str:
    return "Initial outreach" if follow_up_count == 0 else f"Follow-up {follow_up_count}"


def _snapshot(lead: Lead, subject: str, body_html: str, stage: str, sent: bool) -> None:
    """
    Preserves a version of the draft -- either because it actually went out
    (sent=True) or because it was pre-generated/edited and then superseded
    before ever being sent (sent=False, e.g. a reply arrived first).
    """
    if not subject and not body_html:
        return
    lead.draft_history.append(DraftVersion(
        subject=subject, body_html=body_html,
        source=DraftVersionSource.pipeline, stage=stage, sent=sent,
    ))


def _last_sent_content(lead: Lead) -> tuple[str, str]:
    """Most recent version actually emailed (for prompt context) -- falls back to the current draft."""
    for version in reversed(lead.draft_history):
        if version.sent:
            return version.subject, version.body_html
    return lead.current_draft_subject or "", lead.current_draft_body_html or ""


def snapshot_pending_draft_as_unsent(lead: Lead) -> None:
    """
    If a follow-up was pre-drafted but never actually sent (draft_locked is
    False), preserve it before something else overwrites current_draft_* --
    a reply came in, so that pre-drafted follow-up is about to become
    irrelevant, but it shouldn't just vanish.
    """
    if lead.draft_locked:
        return  # current content already represents something that WAS sent (already snapshotted then)
    _snapshot(lead, lead.current_draft_subject or "", lead.current_draft_body_html or "",
              _stage_label(lead.follow_up_count), sent=False)


def stop_outreach_permanently(lead: Lead, new_email_status: EmailStatus) -> None:
    """
    Bounced or unsubscribed (§8): stop ALL further outreach, permanently.
    Used by both the automatic bounce detection (see app/pipeline/
    send_batch.py) and the real unsubscribe-link confirmation (routes_public.py).

    Preserves any pending pre-drafted follow-up (marked unsent) instead of
    leaving it silently sitting there unlocked as if it were still valid to
    send -- send_approved_batch already refuses to actually email a bounced/
    unsubscribed lead, but the dashboard should visibly reflect that nothing
    more is coming, not show a stale editable draft next to a status that
    says otherwise.

    Qualification (staff instruction): not_qualified is only ever set
    automatically in four cases -- both outcomes this function handles
    (unsubscribed, bounced) are two of them. The other two live in the
    classify_reply/exhaust nodes below: a not_interested reply, and
    exhaustion (no reply after all follow-ups).
    """
    snapshot_pending_draft_as_unsent(lead)
    lead.email_status = new_email_status
    lead.draft_locked = True
    if new_email_status == EmailStatus.unsubscribed:
        lead.qualified_status = QualifiedStatus.not_qualified
        lead.qualified_status_source = QualifiedStatusSource.ai
        lead.qualified_status_reasoning = "Lead unsubscribed."
    elif new_email_status == EmailStatus.bounced:
        lead.qualified_status = QualifiedStatus.not_qualified
        lead.qualified_status_source = QualifiedStatusSource.ai
        lead.qualified_status_reasoning = "Email address bounced -- undeliverable."


def _advance_or_lock(lead: Lead) -> None:
    """
    Call right after a send. Pre-drafts the next stage so staff see it ahead
    of time, unless there's nothing more coming (terminal classification, or
    the follow-up cap is reached) -- in which case the just-sent content
    stays as the visible (locked) "current draft" and nothing more is
    generated.
    """
    no_more_stages = (
        lead.ai_response_classification in (AIResponseClassification.interested, AIResponseClassification.not_interested)
        or lead.follow_up_count >= settings.max_follow_ups
    )
    if no_more_stages:
        lead.draft_locked = True
        return
    prev_subject, prev_body = _last_sent_content(lead)
    draft = llm_service.generate_follow_up_draft(lead, prev_subject, prev_body)
    lead.current_draft_subject = draft.subject
    lead.current_draft_body_html = draft.body_html
    lead.draft_locked = False


def record_reply(lead: Lead, reply: InboundReply) -> None:
    """Stores the lead's latest reply verbatim (naive UTC, like every other timestamp)."""
    lead.last_reply_text = reply.body_text.strip()
    received = reply.received_at
    lead.last_reply_at = received.astimezone(timezone.utc).replace(tzinfo=None) if received.tzinfo else received


class TickState(TypedDict, total=False):
    lead: Lead
    inbound_reply: Optional[InboundReply]
    action: str  # what happened this tick, for logging/telemetry
    company_notification_sent: bool


def _is_stopped(lead: Lead) -> bool:
    """§8: unsubscribed/bounced/exhausted leads get no further mail, ever."""
    return lead.email_status in (EmailStatus.bounced, EmailStatus.unsubscribed, EmailStatus.exhausted)


def build_outreach_graph(gmail: GmailClientBase, store: LeadStoreBase):
    graph = StateGraph(TickState)

    def check_stopped(state: TickState) -> TickState:
        state["action"] = "stopped" if _is_stopped(state["lead"]) else "active"
        return state

    def route_after_stopped_check(state: TickState) -> str:
        return "end" if state["action"] == "stopped" else "check_reply"

    def check_reply(state: TickState) -> TickState:
        # inbound_reply is injected by the caller (from a Gmail poll matched
        # to this lead's thread_id) rather than polled per-lead here, so one
        # Gmail poll per tick cycle serves all leads.
        if state.get("inbound_reply") is not None:
            state["action"] = "has_reply"
        else:
            state["action"] = "no_reply"
        return state

    def route_after_reply_check(state: TickState) -> str:
        return "classify_reply" if state["action"] == "has_reply" else "check_follow_up_timing"

    def classify_reply(state: TickState) -> TickState:
        lead = state["lead"]
        reply = state["inbound_reply"]
        result = llm_service.classify_reply(lead, reply.body_text)

        # Whatever the classification, keep the lead's actual words so staff
        # can read them next to the AI's read of them.
        record_reply(lead, reply)

        if result.classification == "not_interested":
            snapshot_pending_draft_as_unsent(lead)
            lead.ai_response_classification = AIResponseClassification.not_interested
            lead.draft_locked = True
            # One of exactly four cases that auto-set not_qualified (staff
            # instruction) -- a clear decline. The other three: exhausted (see
            # exhaust() below), unsubscribed, and bounced (both in
            # stop_outreach_permanently).
            lead.qualified_status = QualifiedStatus.not_qualified
            lead.qualified_status_source = QualifiedStatusSource.ai
            lead.qualified_status_reasoning = "Lead replied not interested."
            state["action"] = "classified_not_interested"

        elif result.classification == "unclear":
            snapshot_pending_draft_as_unsent(lead)
            lead.ai_response_classification = AIResponseClassification.unclear
            prev_subject, prev_body = _last_sent_content(lead)
            draft = llm_service.generate_unclear_reply_draft(lead, prev_subject, prev_body)
            rendered_html = render_email_html(lead, draft.subject, draft.body_html)
            sent = gmail.send(lead.email, draft.subject, rendered_html,
                               thread_id=lead.thread_id, in_reply_to=reply.message_id)
            _snapshot(lead, draft.subject, draft.body_html, "Reply to unclear response", sent=True)
            lead.thread_id = sent.thread_id
            lead.last_sent_at = datetime.utcnow()
            lead.email_status = EmailStatus.sent
            # Doesn't consume a follow-up slot -- just re-enters the normal
            # flow (§6.7) -- so the next pre-drafted stage is still "Follow-up
            # {follow_up_count + 1}", unaffected by this exchange.
            _advance_or_lock(lead)
            state["action"] = "classified_unclear_resent"

        else:  # interested
            snapshot_pending_draft_as_unsent(lead)
            lead.ai_response_classification = AIResponseClassification.interested
            lead.requested_availability = result.requested_availability
            lead.draft_locked = True
            # qualified_status is deliberately left untouched here (staff
            # instruction): no more AI signal-weighing (homeownership,
            # distance, budget, etc.) on an interested reply. It stays
            # whatever it already was -- "unset" for a first reply -- until
            # staff confirms "Qualified" themselves from the dashboard, or
            # it's auto-set not_qualified by one of the three cases above.

            notification = llm_service.generate_company_notification_email(lead, result, reply.body_text)
            if settings.company_notification_email:
                gmail.send(settings.company_notification_email, notification.subject, notification.body_html)
            state["company_notification_sent"] = True
            state["action"] = "classified_interested_notified"

        store.save(lead)
        return state

    def check_follow_up_timing(state: TickState) -> TickState:
        lead = state["lead"]
        if lead.email_status != EmailStatus.sent or lead.last_sent_at is None:
            state["action"] = "not_awaiting_reply"
            return state

        due_at = lead.last_sent_at + timedelta(days=schedule_service.wait_days_for(lead))
        if datetime.utcnow() < due_at:
            state["action"] = "waiting"
            return state

        if lead.follow_up_count < settings.max_follow_ups:
            state["action"] = "send_follow_up"
        else:
            state["action"] = "exhaust"
        return state

    def route_after_timing(state: TickState) -> str:
        if state["action"] == "send_follow_up":
            return "send_follow_up"
        if state["action"] == "exhaust":
            return "exhaust"
        return "end"

    def send_follow_up(state: TickState) -> TickState:
        # The wait window just passed -- send whatever's already pre-drafted
        # (possibly staff-edited) rather than generating something fresh.
        send_current_draft(state["lead"], gmail, store)
        state["action"] = "follow_up_sent"
        return state

    def exhaust(state: TickState) -> TickState:
        lead = state["lead"]
        lead.email_status = EmailStatus.exhausted
        # One of exactly four cases that auto-set not_qualified (staff
        # instruction) -- no reply after every follow-up. The other three:
        # a not_interested reply, unsubscribed, and bounced.
        lead.qualified_status = QualifiedStatus.not_qualified
        lead.qualified_status_source = QualifiedStatusSource.ai
        lead.qualified_status_reasoning = "No response after all follow-ups."
        store.save(lead)
        state["action"] = "exhausted"
        return state

    graph.add_node("check_stopped", check_stopped)
    graph.add_node("check_reply", check_reply)
    graph.add_node("classify_reply", classify_reply)
    graph.add_node("check_follow_up_timing", check_follow_up_timing)
    graph.add_node("send_follow_up", send_follow_up)
    graph.add_node("exhaust", exhaust)

    graph.set_entry_point("check_stopped")
    graph.add_conditional_edges("check_stopped", route_after_stopped_check,
                                 {"end": END, "check_reply": "check_reply"})
    graph.add_conditional_edges("check_reply", route_after_reply_check,
                                 {"classify_reply": "classify_reply",
                                  "check_follow_up_timing": "check_follow_up_timing"})
    graph.add_edge("classify_reply", END)
    graph.add_conditional_edges("check_follow_up_timing", route_after_timing,
                                 {"send_follow_up": "send_follow_up", "exhaust": "exhaust", "end": END})
    graph.add_edge("send_follow_up", END)
    graph.add_edge("exhaust", END)

    return graph.compile()


def run_tick_for_lead(lead: Lead, inbound_reply: Optional[InboundReply],
                       gmail: GmailClientBase, store: LeadStoreBase) -> str:
    """
    Runs one graph pass for a single lead; returns the action taken (for logs).

    Hydrates first: tick.py now passes leads fetched via list_all_light()
    here (draft_history/current_draft_body_html defaulted, not their real
    values -- see storage_service.hydrate_lead), since this is only called
    for leads actually due for graph processing. Every path through the
    graph either reads current_draft_body_html (to send a follow-up) or
    appends to draft_history (snapshotting before a reply overwrites the
    pending draft) -- hydrating here, once, centrally, means no graph node
    has to remember to do it, and no save() from any of them can silently
    overwrite real history with the un-hydrated default.
    """
    hydrate_lead(lead, store)
    app = build_outreach_graph(gmail, store)
    result = app.invoke({"lead": lead, "inbound_reply": inbound_reply})
    return result["action"]


def send_current_draft(lead: Lead, gmail: GmailClientBase, store: LeadStoreBase) -> Lead:
    """
    Sends whatever is currently drafted -- the approved initial email, or a
    pre-drafted follow-up staff is sending early ("Approve & send" is the
    same action at every stage now, not just the first). Whichever it is,
    immediately pre-drafts the next stage afterward (see _advance_or_lock)
    so there's always something to review ahead of the next scheduled send.
    """
    if not lead.current_draft_subject or not lead.current_draft_body_html:
        raise ValueError(f"Lead {lead.id} has no draft to send.")

    prev_subject = lead.current_draft_subject
    prev_body = lead.current_draft_body_html
    is_first_send = lead.last_sent_at is None
    stage = _stage_label(lead.follow_up_count)

    rendered_html = render_email_html(lead, prev_subject, prev_body)
    sent = gmail.send(lead.email, prev_subject, rendered_html, thread_id=lead.thread_id)

    _snapshot(lead, prev_subject, prev_body, stage, sent=True)
    lead.thread_id = sent.thread_id
    lead.last_sent_at = datetime.utcnow()
    lead.email_status = EmailStatus.sent
    if not is_first_send:
        lead.follow_up_count += 1

    # The email is now irreversibly sent -- everything from here on is
    # best-effort pre-staging, not part of "did the send happen." A failure
    # in _advance_or_lock (an LLM call -- can hit a rate limit, an outage,
    # or a billing/quota problem) must never cost us the save below: losing
    # it here previously meant email_status/thread_id/last_sent_at were
    # never persisted even though gmail.send() above had already gone out
    # for real -- the lead still looked "not sent" and safe to retry, so a
    # second attempt would re-send the same email to the same real inbox.
    # (Caught 2026-09-10 via a duplicate real send once OpenAI ran out of
    # credits mid-batch.) On failure here we lock the draft rather than
    # leave the just-sent content sitting as if it were a fresh pending
    # draft, so nothing can be accidentally re-sent either.
    try:
        _advance_or_lock(lead)
    except Exception:  # noqa: BLE001 -- pre-staging the next stage must never undo a send that already happened
        logger.exception(
            "Pre-drafting the next stage failed for lead %s after a successful send; "
            "the send itself is still being recorded.", lead.id,
        )
        lead.draft_locked = True

    store.save(lead)
    return lead


# Back-compat names -- both now just call the unified sender above; kept so
# call sites/tests that distinguish "the very first send" from "a follow-up"
# don't need to change, even though the underlying behavior is identical
# (send_current_draft auto-detects which one it is from last_sent_at).
send_initial_outreach = send_current_draft
send_follow_up_now = send_current_draft


def next_follow_up_due_at(lead: Lead) -> Optional[datetime]:
    """
    When the automatic follow-up loop (§8) will next fire for this lead, or
    None if it isn't waiting on one (terminal reply classification, not
    sent, already exhausted the follow-up count, etc). Always computed from
    the CURRENT last_sent_at -- so sending a follow-up early (send_current_
    draft, via "Approve & send") automatically pushes this forward by
    the scheduled wait from whenever it actually went out, not the
    originally-scheduled time.
    """
    if lead.email_status != EmailStatus.sent or lead.last_sent_at is None:
        return None
    if lead.ai_response_classification in (AIResponseClassification.interested, AIResponseClassification.not_interested):
        return None  # terminal -- automatic follow-up loop stops (§6.7)
    if lead.follow_up_count >= settings.max_follow_ups:
        return None
    return lead.last_sent_at + timedelta(days=schedule_service.wait_days_for(lead))
