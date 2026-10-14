"""
End-to-end pipeline test against synthetic leads and mocked LLM + Gmail +
storage, covering CLAUDE.md §13.1: exercise the full flow before touching
real data or live APIs.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.lead import AIResponseClassification, DraftVersion, EmailStatus, Lead, QualifiedStatus, QualifiedStatusSource
from app.pipeline import draft_generation, outreach_graph, send_batch
from app.pipeline.tick import _is_genuinely_new, run_pipeline_tick
from app.services import llm_service
from app.services.gmail_service import InboundReply, MockGmailClient
from app.services.storage_service import InMemoryLeadStore, InMemoryPipelineStateStore


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


@pytest.fixture
def store():
    return InMemoryLeadStore()


@pytest.fixture
def gmail():
    return MockGmailClient()


@pytest.fixture
def state_store():
    return InMemoryPipelineStateStore()


def test_draft_generation_and_send(store, gmail, monkeypatch):
    monkeypatch.setattr(llm_service, "generate_initial_draft", lambda lead: llm_service.DraftOutput(
        subject="Let's talk about growing your business", body_html="<p>Hi there</p>"
    ))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Follow-up 1 subject", body_html="<p>Follow-up 1 body</p>"
    ))
    lead = make_lead()
    store.save(lead)

    draft_generation.generate_draft_for_lead(lead, store)
    assert lead.current_draft_subject == "Let's talk about growing your business"

    sent_lead = outreach_graph.send_initial_outreach(lead, gmail, store)
    assert sent_lead.email_status == EmailStatus.sent
    assert sent_lead.thread_id is not None
    assert len(gmail.sent_log) == 1

    # What actually goes out is the branded shell wrapping the inner
    # content -- not the raw inner content stored on the lead.
    sent_html = gmail.sent_log[0]["html_body"]
    assert "<p>Hi there</p>" in sent_html
    assert "Unsubscribe" in sent_html

    # The initial email is preserved in history, and follow-up 1 is
    # immediately pre-drafted (unlocked) as the new current draft -- staff
    # see what's coming next rather than only finding out once it fires.
    assert sent_lead.draft_locked is False
    assert sent_lead.current_draft_subject == "Follow-up 1 subject"
    assert sent_lead.current_draft_body_html == "<p>Follow-up 1 body</p>"
    assert len(sent_lead.draft_history) == 1
    assert sent_lead.draft_history[0].subject == "Let's talk about growing your business"
    assert sent_lead.draft_history[0].stage == "Initial outreach"
    assert sent_lead.draft_history[0].sent is True


def test_send_is_recorded_even_if_pre_drafting_the_next_stage_fails(store, gmail, monkeypatch):
    """
    Regression (2026-09-10): a real duplicate send happened when OpenAI ran
    out of credits mid-batch. send_current_draft calls gmail.send() first,
    then pre-drafts the next stage (an LLM call) before store.save() -- if
    that LLM call raised, the exception propagated past store.save()
    entirely, so email_status/thread_id/last_sent_at were never persisted
    even though the email had already gone out for real. The lead still
    looked "not sent" and safe to retry, so a second attempt re-sent the
    same email to the same real inbox. The send must be persisted
    regardless of whether pre-drafting the next stage succeeds.
    """
    monkeypatch.setattr(llm_service, "generate_initial_draft", lambda lead: llm_service.DraftOutput(
        subject="Let's talk about growing your business", body_html="<p>Hi there</p>"
    ))

    def _boom(lead, prev_subject, prev_body):
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(llm_service, "generate_follow_up_draft", _boom)
    lead = make_lead()
    store.save(lead)
    draft_generation.generate_draft_for_lead(lead, store)

    sent_lead = outreach_graph.send_initial_outreach(lead, gmail, store)

    # The email still went out...
    assert len(gmail.sent_log) == 1
    # ...and critically, that fact is actually persisted, not lost.
    assert sent_lead.email_status == EmailStatus.sent
    assert sent_lead.thread_id is not None
    assert sent_lead.last_sent_at is not None
    assert store.get(lead.id).email_status == EmailStatus.sent
    assert store.get(lead.id).thread_id is not None

    # No half-generated/stale draft is left looking like a fresh one that's
    # safe to send -- the lead is locked until staff/a later retry gives it
    # a real next-stage draft.
    assert sent_lead.draft_locked is True


def test_follow_up_fires_after_wait_window_and_stops_at_max(store, gmail, monkeypatch):
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Just checking in", body_html="<p>Following up</p>"
    ))
    lead = make_lead(
        email_status=EmailStatus.sent,
        thread_id="thread-1",
        last_sent_at=datetime.utcnow() - timedelta(days=4),
        follow_up_count=0,
        # Realistic state: a follow-up was already pre-drafted (unlocked)
        # right after the initial send -- see outreach_graph._advance_or_lock.
        current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Pre-drafted</p>",
        draft_locked=False,
    )
    store.save(lead)

    action = outreach_graph.run_tick_for_lead(lead, None, gmail, store)
    assert action == "follow_up_sent"
    assert lead.follow_up_count == 1
    assert len(gmail.sent_log) == 1
    # It sent the pre-drafted content, then pre-drafted follow-up 2 (same
    # mock, so identical content, but still unlocked -- more is coming).
    assert lead.draft_locked is False
    assert lead.draft_history[-1].subject == "Pre-drafted follow-up 1"
    assert lead.draft_history[-1].sent is True

    # Not due yet right after sending -> no-op.
    action2 = outreach_graph.run_tick_for_lead(lead, None, gmail, store)
    assert action2 == "waiting"

    # Simulate reaching max follow-ups with no reply.
    lead.follow_up_count = 3
    lead.last_sent_at = datetime.utcnow() - timedelta(days=4)
    action3 = outreach_graph.run_tick_for_lead(lead, None, gmail, store)
    assert action3 == "exhausted"
    assert lead.email_status == EmailStatus.exhausted


def test_send_follow_up_now_moves_last_sent_at_and_advances_next_due(store, gmail, monkeypatch):
    """
    Staff "send now" (an early follow-up) must push last_sent_at to when it
    actually went out -- so next_follow_up_due_at for the *next* follow-up
    recomputes from there, not from the originally-scheduled +3-day mark.
    """
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Checking in", body_html="<p>Hi</p>"
    ))
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-early",
                      last_sent_at=datetime.utcnow() - timedelta(hours=2), follow_up_count=0,
                      current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Pre-drafted</p>",
                      draft_locked=False)
    store.save(lead)

    before_send = datetime.utcnow()
    outreach_graph.send_follow_up_now(lead, gmail, store)

    assert lead.follow_up_count == 1
    assert lead.last_sent_at >= before_send  # moved to "now", not the old schedule
    next_due = outreach_graph.next_follow_up_due_at(lead)
    assert next_due is not None
    assert next_due >= before_send + timedelta(days=3) - timedelta(seconds=2)


def test_next_follow_up_due_at_none_when_terminal_or_exhausted():
    interested_lead = make_lead(email_status=EmailStatus.sent, last_sent_at=datetime.utcnow(),
                                 ai_response_classification="interested")
    assert outreach_graph.next_follow_up_due_at(interested_lead) is None

    exhausted_count_lead = make_lead(email_status=EmailStatus.sent, last_sent_at=datetime.utcnow(), follow_up_count=3)
    assert outreach_graph.next_follow_up_due_at(exhausted_count_lead) is None

    not_sent_lead = make_lead(email_status=EmailStatus.not_sent)
    assert outreach_graph.next_follow_up_due_at(not_sent_lead) is None


def test_next_follow_up_due_at_is_last_sent_plus_wait_days():
    sent_at = datetime.utcnow()
    lead = make_lead(email_status=EmailStatus.sent, last_sent_at=sent_at, follow_up_count=0)
    due = outreach_graph.next_follow_up_due_at(lead)
    assert due == sent_at + timedelta(days=3)  # the default schedule


def test_stopped_lead_never_gets_touched_again(store, gmail):
    lead = make_lead(email_status=EmailStatus.unsubscribed, thread_id="thread-2",
                      last_sent_at=datetime.utcnow() - timedelta(days=10))
    store.save(lead)
    action = outreach_graph.run_tick_for_lead(lead, None, gmail, store)
    assert action == "stopped"
    assert len(gmail.sent_log) == 0


def test_interested_reply_no_longer_auto_proposes_qualification(store, gmail, monkeypatch):
    """
    Staff instruction: qualification tracks engagement, not AI signal-
    weighing (homeownership, distance, budget, etc.) on the reply content.
    An interested reply notifies the company and captures availability/
    address, but leaves qualified_status for staff to decide -- it must NOT
    auto-flip to qualified just because the reply reads as interested.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-3")
    store.save(lead)

    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="interested",
        requested_availability="Weekday evenings",
    ))
    monkeypatch.setattr(llm_service, "generate_company_notification_email", lambda lead, result, text: llm_service.DraftOutput(
        subject="New interested lead", body_html="<p>Summary</p>"
    ))

    reply = InboundReply(thread_id="thread-3", from_email=lead.email,
                          body_text="Yes I'm interested, evenings work best!", message_id="m1")
    action = outreach_graph.run_tick_for_lead(lead, reply, gmail, store)

    assert action == "classified_interested_notified"
    assert lead.qualified_status == QualifiedStatus.unset  # left for staff, not AI-proposed
    assert lead.qualified_status_source is None
    assert lead.requested_availability == "Weekday evenings"
    assert lead.last_reply_text == "Yes I'm interested, evenings work best!"  # the reply itself is kept
    assert lead.last_reply_at is not None and lead.last_reply_at.tzinfo is None


def test_not_interested_reply_auto_sets_not_qualified(store, gmail, monkeypatch):
    """
    One of exactly three cases that auto-set not_qualified (staff
    instruction): a clear decline. The other two are exhaustion (no reply
    after all follow-ups) and unsubscribe.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-4")
    store.save(lead)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="not_interested"
    ))
    reply = InboundReply(thread_id="thread-4", from_email=lead.email, body_text="Not interested, thanks",
                          message_id="m2")
    action = outreach_graph.run_tick_for_lead(lead, reply, gmail, store)
    assert action == "classified_not_interested"
    assert lead.qualified_status == QualifiedStatus.not_qualified
    assert lead.qualified_status_source.value == "ai"


def test_unclear_reply_resends_in_same_style_without_consuming_follow_up_count(store, gmail, monkeypatch):
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-5", follow_up_count=1)
    store.save(lead)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="unclear"
    ))
    monkeypatch.setattr(llm_service, "generate_unclear_reply_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Following up", body_html="<p>Still there?</p>"
    ))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Next follow-up", body_html="<p>Next</p>"
    ))
    reply = InboundReply(thread_id="thread-5", from_email=lead.email, body_text="huh?", message_id="m3")
    action = outreach_graph.run_tick_for_lead(lead, reply, gmail, store)
    assert action == "classified_unclear_resent"
    assert lead.follow_up_count == 1  # unchanged -- doesn't consume a follow-up slot
    assert len(gmail.sent_log) == 1


def test_pending_pregenerated_draft_preserved_as_unsent_when_unclear_reply_arrives(store, gmail, monkeypatch):
    """
    Regression coverage for the draft-ahead model: a follow-up gets
    pre-drafted (unlocked, never sent) right after the previous stage sends.
    If a reply arrives before that pre-draft ever goes out, it must not just
    be silently discarded -- it's preserved in draft_history marked
    sent=False ("(unsent)" in the dashboard), and the reply-driven draft
    takes over as current.
    """
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-pending", follow_up_count=1,
        current_draft_subject="Pre-drafted follow-up 2", current_draft_body_html="<p>Never sent</p>",
        draft_locked=False,
    )
    store.save(lead)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="unclear"
    ))
    monkeypatch.setattr(llm_service, "generate_unclear_reply_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Reply to unclear", body_html="<p>Responding</p>"
    ))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Next follow-up", body_html="<p>Next</p>"
    ))
    reply = InboundReply(thread_id="thread-pending", from_email=lead.email, body_text="huh?", message_id="m-pending")

    outreach_graph.run_tick_for_lead(lead, reply, gmail, store)

    # Two history entries: the pending pre-draft that never went out
    # (preserved, marked unsent), and the reply response itself, which DID
    # go out immediately (unclear replies still auto-send).
    assert len(lead.draft_history) == 2
    assert lead.draft_history[0].subject == "Pre-drafted follow-up 2"
    assert lead.draft_history[0].sent is False
    assert lead.draft_history[1].subject == "Reply to unclear"
    assert lead.draft_history[1].sent is True
    assert lead.current_draft_subject != "Pre-drafted follow-up 2"  # overwritten by the reply-driven draft


def test_pending_pregenerated_draft_preserved_as_unsent_when_lead_qualifies(store, gmail, monkeypatch):
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-pending-2", follow_up_count=0,
        current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Never sent</p>",
        draft_locked=False,
    )
    store.save(lead)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="interested", qualified_status="qualified", qualification_reasoning="Good fit",
    ))
    monkeypatch.setattr(llm_service, "generate_company_notification_email", lambda lead, result, text: llm_service.DraftOutput(
        subject="Lead update", body_html="<p>Summary</p>"
    ))
    reply = InboundReply(thread_id="thread-pending-2", from_email=lead.email, body_text="Yes!", message_id="m-p2")

    outreach_graph.run_tick_for_lead(lead, reply, gmail, store)

    assert len(lead.draft_history) == 1
    assert lead.draft_history[0].subject == "Pre-drafted follow-up 1"
    assert lead.draft_history[0].sent is False
    assert lead.draft_locked is True  # terminal -- nothing more auto-drafted


def test_bounce_detection_stops_all_further_outreach(store, gmail):
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-6")
    store.save(lead)
    bounce = InboundReply(thread_id="thread-6", from_email="mailer-daemon@googlemail.com",
                           body_text="Delivery failed", message_id="m4")
    outcome = send_batch.process_inbound_for_bounce_or_unsubscribe(bounce, lead, store)
    assert outcome == "bounced"
    assert lead.email_status == EmailStatus.bounced


def test_reply_containing_the_word_unsubscribe_does_not_auto_unsubscribe(store, gmail):
    """
    Regression test for a real production bug: unsubscribing used to also be
    triggered by scanning reply text for keywords like "unsubscribe". Every
    outbound email's own footer contains that exact word, and quote-
    stripping a real reply is unreliable enough across mail clients that the
    footer routinely survived into body_text -- so almost *any* reply at all
    false-positived as an unsubscribe request. An entire batch of leads got
    auto-unsubscribed the first time any of them simply replied normally.
    The signed link (routes_public.py) is now the only way to unsubscribe;
    reply text -- even text that happens to contain "unsubscribe" -- must
    never flip email_status on its own.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-7")
    store.save(lead)
    msg = InboundReply(
        thread_id="thread-7", from_email=lead.email, message_id="m5",
        body_text="Sounds great, let's talk!\n\nOn Tue, Sep 2, 2026, VRT Management Group wrote:\n"
                   "> ...Don't want to hear from us again? Unsubscribe...",
    )
    outcome = send_batch.process_inbound_for_bounce_or_unsubscribe(msg, lead, store)
    assert outcome is None
    assert lead.email_status == EmailStatus.sent


def test_bounce_locks_and_preserves_pending_pregenerated_draft(store, gmail):
    """
    Regression test: a bounce used to only flip email_status -- a follow-up
    that had already been pre-drafted (unlocked, never sent) was left
    sitting there untouched, still showing as an editable "upcoming" draft
    even though the address is dead and nothing should ever go to it again.
    """
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-bounce",
        current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Never sent</p>",
        draft_locked=False,
    )
    store.save(lead)
    bounce = InboundReply(thread_id="thread-bounce", from_email="mailer-daemon@googlemail.com",
                           body_text="Delivery failed", message_id="m-bounce")

    outcome = send_batch.process_inbound_for_bounce_or_unsubscribe(bounce, lead, store)

    assert outcome == "bounced"
    assert lead.email_status == EmailStatus.bounced
    assert lead.draft_locked is True  # no longer shown as an editable upcoming draft
    assert len(lead.draft_history) == 1
    assert lead.draft_history[0].subject == "Pre-drafted follow-up 1"
    assert lead.draft_history[0].sent is False


def test_unsubscribe_locks_and_preserves_pending_pregenerated_draft(store, gmail):
    """
    Unsubscribing only ever happens via the signed link now (routes_public.py
    calls stop_outreach_permanently directly) -- exercise that same function
    here rather than through reply text.
    """
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-unsub-pending",
        current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Never sent</p>",
        draft_locked=False,
    )
    store.save(lead)

    outreach_graph.stop_outreach_permanently(lead, EmailStatus.unsubscribed)
    store.save(lead)

    assert lead.email_status == EmailStatus.unsubscribed
    assert lead.draft_locked is True
    assert len(lead.draft_history) == 1
    assert lead.draft_history[0].sent is False
    # One of exactly three cases that auto-set not_qualified (staff instruction).
    assert lead.qualified_status == QualifiedStatus.not_qualified
    assert lead.qualified_status_source.value == "ai"


def test_bounce_auto_sets_not_qualified(store, gmail):
    """
    One of exactly four cases that auto-set not_qualified (staff
    instruction): a dead address can never be qualified either.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-bounce-qual")
    store.save(lead)

    outreach_graph.stop_outreach_permanently(lead, EmailStatus.bounced)

    assert lead.email_status == EmailStatus.bounced
    assert lead.qualified_status == QualifiedStatus.not_qualified
    assert lead.qualified_status_source.value == "ai"


def test_exhaustion_auto_sets_not_qualified(store, gmail):
    """
    One of exactly three cases that auto-set not_qualified (staff
    instruction): no reply after every follow-up.
    """
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-exhaust-qual",
        follow_up_count=3, last_sent_at=datetime.utcnow() - timedelta(days=10),
    )
    store.save(lead)

    action = outreach_graph.run_tick_for_lead(lead, None, gmail, store)

    assert action == "exhausted"
    assert lead.email_status == EmailStatus.exhausted
    assert lead.qualified_status == QualifiedStatus.not_qualified
    assert lead.qualified_status_source.value == "ai"


def test_tick_dedup_ignores_stale_replies_not_filtered_by_gmail_poll():
    """
    Regression test: gmail_service.poll_new_replies() is intentionally NOT
    filtered to unread/new messages (a dev/staff account marks messages read
    just by opening them), so /pipeline/tick must dedup itself by comparing
    each reply's received_at against the lead's last_sent_at -- otherwise the
    same historical reply gets reclassified (and the company re-notified)
    on every single tick.
    """
    last_sent_at = datetime(2026, 8, 20, 12, 0, 0)

    stale_reply = InboundReply(
        thread_id="t1", from_email="lead@example.com", body_text="old reply",
        message_id="m-old", received_at=(last_sent_at - timedelta(hours=1)).replace(tzinfo=timezone.utc),
    )
    assert _is_genuinely_new(stale_reply, last_sent_at) is False

    fresh_reply = InboundReply(
        thread_id="t1", from_email="lead@example.com", body_text="new reply",
        message_id="m-new", received_at=(last_sent_at + timedelta(hours=1)).replace(tzinfo=timezone.utc),
    )
    assert _is_genuinely_new(fresh_reply, last_sent_at) is True

    # A reply landing within the same few seconds as our own send (e.g.
    # Gmail's own inbox copy of the outbound message) must not count either.
    same_moment_reply = InboundReply(
        thread_id="t1", from_email="lead@example.com", body_text="echo",
        message_id="m-echo", received_at=last_sent_at.replace(tzinfo=timezone.utc),
    )
    assert _is_genuinely_new(same_moment_reply, last_sent_at) is False

    # No prior send recorded -> treat as new (shouldn't happen in practice
    # since a lead only gets a thread_id after a send, but fail open here
    # rather than silently dropping a reply).
    assert _is_genuinely_new(fresh_reply, None) is True


def test_tick_scopes_gmail_poll_to_active_lead_threads_only(store, gmail, state_store, monkeypatch):
    """
    Regression test for a real production bug: the tick used to poll
    Gmail's *entire inbox* every cycle and then query the database once per
    message found there -- including mail with nothing to do with any lead
    (job alerts, receipts, etc. on a personal Gmail account). That's one
    database round-trip per unrelated email, every tick, forever. The poll
    must be scoped to only the threads of leads actually worth checking:
    sent, and not already at a terminal reply classification.
    """
    active_lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-active")
    terminal_lead = make_lead(
        email="other@example.com", thread_id="thread-terminal",
        email_status=EmailStatus.sent, ai_response_classification="interested",
    )
    not_sent_lead = make_lead(email="third@example.com", email_status=EmailStatus.not_sent, thread_id=None)
    store.save(active_lead)
    store.save(terminal_lead)
    store.save(not_sent_lead)

    captured = {}
    original_poll = gmail.poll_new_replies

    def spy_poll(thread_ids):
        captured["thread_ids"] = thread_ids
        return original_poll(thread_ids)

    monkeypatch.setattr(gmail, "poll_new_replies", spy_poll)

    run_pipeline_tick(gmail, store, state_store)

    assert captured["thread_ids"] == {"thread-active"}


def test_tick_still_polls_unsubscribed_leads_thread_and_flags_a_late_reply(store, gmail, state_store, monkeypatch):
    """
    Unsubscribing stops outreach *to* the lead, not monitoring of a thread
    they might still reply on -- a real reply after unsubscribing must
    still surface to staff (the actual message, plus an AI read on it).
    It must never trigger a draft or a send *to the lead* (that would break
    the 'no further outreach, ever' guarantee) or change email_status --
    but a genuinely interested late reply does get promoted the same way an
    active lead's would: ai_response_classification set, and staff notified
    by email, so it doesn't sit invisible in notes.
    """
    monkeypatch.setattr(
        llm_service, "classify_reply",
        lambda lead, text: llm_service.ClassificationResult(classification="interested"),
    )
    lead = make_lead(
        email_status=EmailStatus.unsubscribed, thread_id="thread-unsub",
        last_sent_at=datetime.utcnow() - timedelta(days=1),
    )
    store.save(lead)
    gmail.inject_reply("thread-unsub", lead.email, "Wait, I changed my mind, please call me")

    results = run_pipeline_tick(gmail, store, state_store)

    assert len(results) == 1
    assert results[0].action == "late_reply_flagged"
    saved = store.get(lead.id)
    assert saved.email_status == EmailStatus.unsubscribed  # unchanged -- still stopped
    assert saved.ai_response_classification == AIResponseClassification.interested
    assert saved.current_draft_subject is None  # never drafted anything, never sent to the lead
    assert "Wait, I changed my mind, please call me" in saved.notes  # actual message text
    assert "AI read: interested" in saved.notes


def test_late_reply_on_stopped_thread_is_flagged_only_once(store, gmail, monkeypatch):
    """
    A live Gmail poll returns a thread's whole message history every time
    (unlike the mock, which pops a message once matched) -- so the same
    reply will be handed to _flag_late_reply again on every future tick.
    The message_id-embedded marker must stop it from being appended to
    notes more than once.
    """
    from app.pipeline.tick import _flag_late_reply

    monkeypatch.setattr(
        llm_service, "classify_reply",
        lambda lead, text: llm_service.ClassificationResult(classification="unclear"),
    )
    lead = make_lead(email_status=EmailStatus.unsubscribed, thread_id="thread-unsub")
    store.save(lead)
    reply = InboundReply(
        thread_id="thread-unsub", from_email=lead.email, body_text="Still interested actually",
        message_id="msg-1", received_at=datetime.now(timezone.utc),
    )

    _flag_late_reply(lead, reply, store, gmail)
    first_notes = store.get(lead.id).notes

    _flag_late_reply(store.get(lead.id), reply, store, gmail)  # same message_id, simulating a re-poll

    assert store.get(lead.id).notes == first_notes
    assert store.get(lead.id).notes.count("Still interested actually") == 1


def test_tick_light_fetch_does_not_wipe_draft_history_on_a_follow_up_send(store, gmail, state_store, monkeypatch):
    """
    Regression test for the database-read fix (2026-09-30): run_pipeline_tick
    now reads leads via list_all_light(), which comes back with draft_history
    defaulted to [] instead of its real DB value (see
    storage_service.list_all_light / hydrate_lead). A lead due for its next
    follow-up goes through run_tick_for_lead -> send_current_draft, which
    both reads current_draft_body_html (to actually send) and appends a new
    entry to draft_history before saving. Without hydrating first, that save
    would silently overwrite a lead's entire prior draft_history with just
    the one new entry -- real history for that lead gone for good. This
    proves prior entries survive a full tick cycle end to end, not just a
    direct run_tick_for_lead call on an already-full lead object.
    """
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Just checking in", body_html="<p>Following up</p>"
    ))
    lead = make_lead(
        email_status=EmailStatus.sent,
        thread_id="thread-history-safe",
        last_sent_at=datetime.utcnow() - timedelta(days=4),
        follow_up_count=0,
        current_draft_subject="Pre-drafted follow-up 1", current_draft_body_html="<p>Pre-drafted</p>",
        draft_locked=False,
        draft_history=[
            DraftVersion(subject="Initial outreach", body_html="<p>Hi there</p>", source="pipeline", sent=True),
        ],
    )
    store.save(lead)

    results = run_pipeline_tick(gmail, store, state_store)

    assert len(results) == 1
    assert results[0].action == "follow_up_sent"
    saved = store.get(lead.id)
    subjects = [v.subject for v in saved.draft_history]
    assert "Initial outreach" in subjects  # the pre-existing entry survived
    assert "Pre-drafted follow-up 1" in subjects  # the newly-snapshotted one is there too
    assert len(saved.draft_history) == 2


def test_exhausted_lead_replying_interested_reopens_for_review_and_notifies_staff(store, gmail, state_store, monkeypatch):
    """
    Regression test for a real production miss: a lead exhausted after 3
    follow-ups with no reply (auto-set not_qualified, reasoning "No response
    after all follow-ups") later opened the mailbox and replied genuinely
    interested. Because exhaustion's not_qualified was the pipeline's own
    auto-write (qualified_status_source=ai), not a staff decision, the late
    "interested" reply must reopen it: ai_response_classification promoted,
    qualified_status reset to unset so it surfaces in Needs Review, and the
    company notified by email -- exactly like an active lead's interested
    reply, not left sitting invisible in notes.
    """
    monkeypatch.setattr(settings, "company_notification_email", "staff@example.com")
    monkeypatch.setattr(
        llm_service, "classify_reply",
        lambda lead, text: llm_service.ClassificationResult(
            classification="interested", requested_availability="weekday afternoons",
        ),
    )
    lead = make_lead(
        email_status=EmailStatus.exhausted, thread_id="thread-exhausted",
        last_sent_at=datetime.utcnow() - timedelta(days=10),
        follow_up_count=3,
        qualified_status=QualifiedStatus.not_qualified,
        qualified_status_source=QualifiedStatusSource.ai,
        qualified_status_reasoning="No response after all follow-ups.",
    )
    store.save(lead)
    gmail.inject_reply("thread-exhausted", lead.email, "Sorry for the delay -- very interested, let's talk pricing")

    results = run_pipeline_tick(gmail, store, state_store)

    assert len(results) == 1
    assert results[0].action == "late_reply_flagged"
    saved = store.get(lead.id)
    assert saved.email_status == EmailStatus.exhausted  # still stopped, no further outreach to the lead
    assert saved.ai_response_classification == AIResponseClassification.interested
    assert saved.requested_availability == "weekday afternoons"
    assert saved.qualified_status == QualifiedStatus.unset
    assert saved.qualified_status_source is None
    assert saved.current_draft_subject is None  # never drafted/sent anything to the lead
    assert len(gmail._sent) == 1  # the company notification, not a send to the lead
    assert gmail._sent[0]["to"] != lead.email


def test_exhausted_lead_late_reply_leaves_a_staff_confirmed_qualification_alone(store, gmail, state_store, monkeypatch):
    """
    If staff already reviewed and confirmed qualified_status themselves
    (qualified_status_source=staff) before a late reply comes in, the late
    reply must not silently overwrite that human call -- only the pipeline's
    own auto-writes get reset.
    """
    monkeypatch.setattr(
        llm_service, "classify_reply",
        lambda lead, text: llm_service.ClassificationResult(classification="interested"),
    )
    lead = make_lead(
        email_status=EmailStatus.exhausted, thread_id="thread-exhausted-2",
        last_sent_at=datetime.utcnow() - timedelta(days=10),
        follow_up_count=3,
        qualified_status=QualifiedStatus.not_qualified,
        qualified_status_source=QualifiedStatusSource.staff,
    )
    store.save(lead)
    gmail.inject_reply("thread-exhausted-2", lead.email, "Actually, let's talk")

    run_pipeline_tick(gmail, store, state_store)

    saved = store.get(lead.id)
    assert saved.ai_response_classification == AIResponseClassification.interested
    assert saved.qualified_status == QualifiedStatus.not_qualified  # staff call, left untouched
    assert saved.qualified_status_source == QualifiedStatusSource.staff


def test_tick_skips_gmail_poll_entirely_when_no_active_leads(store, gmail, state_store, monkeypatch):
    """No sent/awaiting-reply leads at all -> must not call Gmail, not even with an empty set."""
    called = []
    monkeypatch.setattr(gmail, "poll_new_replies", lambda thread_ids: called.append(thread_ids) or [])

    result = run_pipeline_tick(gmail, store, state_store)

    assert result == []
    assert called == []


def test_terminal_classification_is_never_reprocessed_on_later_ticks(store, gmail, state_store, monkeypatch):
    """
    Regression test for a live bug: gmail_service.poll_new_replies() is not
    filtered to unread/new messages, and last_sent_at never advances again
    once a lead is classified interested/not_interested (§6.7 -- both are
    terminal for the automated pipeline). Without an explicit terminal-state
    guard, every later tick kept re-matching the same old reply, reclassified
    it, and re-sent the company notification email a second, third, ... time.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-terminal")
    store.save(lead)

    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="interested", qualified_status="qualified", qualification_reasoning="ok",
    ))
    notify_calls = []
    monkeypatch.setattr(
        llm_service, "generate_company_notification_email",
        lambda lead, result, text: notify_calls.append(1) or llm_service.DraftOutput(subject="x", body_html="y"),
    )

    reply_time = datetime.utcnow()
    gmail.inject_reply("thread-terminal", lead.email, "Yes, interested!", received_at=reply_time)

    first = run_pipeline_tick(gmail, store, state_store)
    assert [o.action for o in first] == ["classified_interested_notified"]
    assert len(notify_calls) == 1

    # MockGmailClient.poll_new_replies() drains its inbox, unlike the live
    # client's unfiltered time-window poll (see gmail_service.py) which would
    # keep returning the same message every cycle -- so re-inject the exact
    # same reply to faithfully reproduce that behavior here.
    gmail.inject_reply("thread-terminal", lead.email, "Yes, interested!", received_at=reply_time)
    second = run_pipeline_tick(gmail, store, state_store)
    assert second == []
    assert len(notify_calls) == 1


def test_tick_lock_prevents_two_concurrent_ticks_from_ever_overlapping(store, gmail, state_store, monkeypatch):
    """
    The auto-tick runs in a worker thread (asyncio.to_thread); a manual
    "Check replies" click runs on the event loop's own thread -- two real OS
    threads that could otherwise execute at the same moment, each finding
    the same reply and, in the worst case, each independently sending a
    duplicate auto-reply. Proves the lock actually serializes them -- not
    just that it exists -- by making one call slow and confirming the
    second thread's work never starts until the first one has fully
    finished, not merely that the final state came out consistent.
    """
    events: list[tuple[str, str]] = []
    store.save(make_lead(email_status=EmailStatus.sent, thread_id="thread-lock"))  # something worth ticking for
    original_list_due = store.list_due_for_follow_up

    def slow_list_due(now):
        events.append(("start", threading.current_thread().name))
        time.sleep(0.1)
        result = original_list_due(now)
        events.append(("end", threading.current_thread().name))
        return result

    monkeypatch.setattr(store, "list_due_for_follow_up", slow_list_due)

    t1 = threading.Thread(target=run_pipeline_tick, args=(gmail, store, state_store), name="auto-tick")
    t2 = threading.Thread(target=run_pipeline_tick, args=(gmail, store, state_store), name="manual-click")
    t1.start()
    time.sleep(0.02)  # give t1 a head start so it acquires the lock first
    t2.start()
    t1.join()
    t2.join()

    assert events == [
        ("start", "auto-tick"), ("end", "auto-tick"),
        ("start", "manual-click"), ("end", "manual-click"),
    ]


def test_reply_on_an_unrecognized_thread_matches_by_sender_email_and_repoints_thread_id(store, gmail, state_store, monkeypatch):
    """
    Caught live 2026-09-17: a real lead (Sean) replied via Yahoo Mail's
    webmail, which didn't set In-Reply-To/References on the reply -- Gmail
    started a brand-new thread for it, completely disconnected from the
    thread_id stored on his lead. A thread-only lookup would have left his
    reply permanently invisible; the sender-email fallback is what actually
    catches leads whose mail client mishandles threading, not just an edge
    case in theory.
    """
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="interested",
    ))
    monkeypatch.setattr(
        llm_service, "generate_company_notification_email",
        lambda lead, result, text: llm_service.DraftOutput(subject="x", body_html="y"),
    )
    lead = make_lead(email="sean@example.com", email_status=EmailStatus.sent, thread_id="thread-original")
    store.save(lead)
    run_pipeline_tick(gmail, store, state_store)  # bootstrap the cursor, nothing pending yet

    # The reply lands on a thread nothing in the system knows about.
    gmail.inject_reply("thread-brand-new", "sean@example.com", "Yes, starting a new build, interested!")
    outcomes = run_pipeline_tick(gmail, store, state_store)

    assert [o.action for o in outcomes] == ["classified_interested_notified"]
    updated = store.get(lead.id)
    assert updated.ai_response_classification == AIResponseClassification.interested
    # The lead now tracks the thread the reply actually arrived on, so any
    # further correspondence (and re-runs of this same tick) keep working.
    assert updated.thread_id == "thread-brand-new"


def test_reply_from_a_genuinely_unknown_sender_is_discarded_not_matched(store, gmail, state_store):
    """The fallback matches by sender email specifically -- it must not
    match (or crash on) mail from someone who isn't a lead at all."""
    lead = make_lead(email="real-lead@example.com", email_status=EmailStatus.sent, thread_id="thread-real")
    store.save(lead)
    run_pipeline_tick(gmail, store, state_store)  # bootstrap

    gmail.inject_reply("thread-unrelated", "totally-unrelated@example.com", "unrelated mail")
    outcomes = run_pipeline_tick(gmail, store, state_store)

    # The unrelated mail attaches to nothing, and nothing else is due, so the
    # tick has no activity to report (it only reports leads it acted on).
    assert outcomes == []
    assert store.get(lead.id).thread_id == "thread-real"  # untouched


def test_bounce_detected_correctly_through_the_history_cursor_path(store, gmail, state_store):
    """
    Bounce detection runs on whatever poll_new_replies_with_history_cursor
    hands back -- must fire identically whether that message was found via
    the old full poll or the new history.list path. Bootstraps a cursor
    first (steady state), then a bounce arrives on the fast path.
    """
    lead = make_lead(email_status=EmailStatus.sent, thread_id="thread-bounce-history")
    store.save(lead)
    run_pipeline_tick(gmail, store, state_store)  # bootstrap the cursor, nothing pending yet

    gmail.inject_reply("thread-bounce-history", "mailer-daemon@googlemail.com", "Delivery failed")
    outcomes = run_pipeline_tick(gmail, store, state_store)

    assert [o.action for o in outcomes] == ["bounced"]
    assert store.get(lead.id).email_status == EmailStatus.bounced


def test_cold_start_at_scale_finds_every_pending_reply_across_many_leads(store, gmail, state_store, monkeypatch):
    """
    The first tick ever run (no cursor stored) falls back to the old full
    per-thread poll -- exactly what production's first tick after this
    deploys will do, at real scale (hundreds of active leads instead of
    the few in the other tests here). Must not silently drop any of them.
    """
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(
        classification="not_interested"
    ))
    leads = [make_lead(email=f"scale{i}@example.com", email_status=EmailStatus.sent, thread_id=f"thread-scale-{i}")
             for i in range(6)]
    for i, lead in enumerate(leads):
        store.save(lead)
        gmail.inject_reply(f"thread-scale-{i}", lead.email, "Not interested, thanks")

    outcomes = run_pipeline_tick(gmail, store, state_store)

    assert len(outcomes) == 6
    assert {o.action for o in outcomes} == {"classified_not_interested"}
    assert all(store.get(l.id).ai_response_classification == AIResponseClassification.not_interested for l in leads)
    assert state_store.get_gmail_history_id() is not None  # bootstrapped for next time


def test_bulk_send_refuses_a_not_interested_lead_even_though_still_sent(store, gmail):
    """
    Regression test for a real gap: send_approved_batch used to only block
    email_status in (bounced, unsubscribed, exhausted). A lead classified
    not_interested (or interested) keeps email_status == "sent" and its
    current_draft_subject/body_html un-cleared (classify_reply only
    archives the pending draft to history, then sets draft_locked -- it
    doesn't blank the current_draft_* fields) -- so a staff member
    selecting "all leads" and clicking Send would have actually re-sent
    that stale locked draft to someone who should get no further outreach
    at all. draft_locked is the correct guard: every terminal case already
    sets it True.
    """
    lead = make_lead(
        email_status=EmailStatus.sent, thread_id="thread-ni",
        ai_response_classification="not_interested",
        current_draft_subject="Stale follow-up", current_draft_body_html="<p>Stale</p>",
        draft_locked=True,
    )
    store.save(lead)

    result = send_batch.send_approved_batch([lead.id], gmail, store)

    assert result["sent"] == []
    assert len(result["errors"]) == 1
    assert len(gmail.sent_log) == 0  # nothing actually went out
    assert store.get(lead.id).email_status == EmailStatus.sent  # unchanged


def test_daily_send_cap_defers_excess_leads(store, gmail, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "daily_send_cap", 2)
    monkeypatch.setattr(llm_service, "generate_initial_draft", lambda lead: llm_service.DraftOutput(
        subject="Hi", body_html="<p>Hi</p>"
    ))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Follow-up", body_html="<p>Follow-up</p>"
    ))

    leads = [make_lead(email=f"lead{i}@example.com") for i in range(3)]
    for lead in leads:
        draft_generation.generate_draft_for_lead(lead, store)

    result = send_batch.send_approved_batch([l.id for l in leads], gmail, store)
    assert len(result["sent"]) == 2
    assert len(result["deferred_to_next_batch"]) == 1
    assert result["daily_cap"] == 2


def test_bulk_send_throttles_between_real_gmail_calls_but_not_around_skips(store, gmail, monkeypatch):
    """
    Same fix poll_new_replies got earlier -- sending and reply-polling share
    Gmail's per-minute quota, so firing every send in a bulk batch
    back-to-back with zero spacing is exactly what bursts past it. The
    delay should only apply between *actual* sends -- a lead skipped
    because it's locked never touches Gmail, so it must not cost a
    throttle wait either side of it.
    """
    monkeypatch.setattr(llm_service, "generate_initial_draft", lambda lead: llm_service.DraftOutput(
        subject="Hi", body_html="<p>Hi</p>"
    ))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Follow-up", body_html="<p>Follow-up</p>"
    ))
    sendable = [make_lead(email=f"throttle{i}@example.com") for i in range(2)]
    for lead in sendable:
        draft_generation.generate_draft_for_lead(lead, store)
    locked = make_lead(email="locked@example.com", draft_locked=True)
    store.save(locked)

    sleep_calls = []
    monkeypatch.setattr(send_batch.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    # locked lead first, so a naive "sleep before every loop iteration"
    # implementation (wrong -- it never touches Gmail) would still show up
    # as an extra sleep call here if that bug existed.
    lead_ids = [locked.id] + [l.id for l in sendable]
    result = send_batch.send_approved_batch(lead_ids, gmail, store)

    assert len(result["sent"]) == 2
    assert len(sleep_calls) == 1  # one gap, between the two real sends -- none around the skip


def test_leads_sent_today_uses_the_companys_local_day_not_utcs(store):
    """
    Regression (2026-09-13): the company is in Botsford, CT (EDT, UTC-4
    in September). leads_sent_today used naive UTC midnight as the cutoff,
    so the business day rolled over 4 hours early from the company's own
    perspective -- a lead sent at 8pm EDT (still very much "today" locally)
    fell after UTC midnight and got counted as "yesterday" the moment UTC's
    date ticked over, even though it was still evening in Connecticut. Real
    numbers that day: 149 actually sent, only 18 showing as "sent today"
    less than an hour after UTC's rollover.
    """
    # "Now" is 00:56 UTC on the 14th -- 8:56pm EDT on the 13th. Still
    # Sep 13 for the company; already Sep 14 in UTC.
    now = datetime(2026, 9, 14, 0, 56, tzinfo=timezone.utc)

    lead_evening_edt = make_lead(email="evening@example.com")
    # 11:30pm UTC on the 13th = 7:30pm EDT on the 13th -- sent earlier the
    # same *local* business day, but before UTC's own midnight.
    lead_evening_edt.last_sent_at = datetime(2026, 9, 13, 23, 30)
    store.save(lead_evening_edt)

    lead_actually_yesterday = make_lead(email="yesterday@example.com")
    # 4pm EDT on the 12th (8pm UTC) -- genuinely the previous local day,
    # well before the company's Sep 13 midnight (= 4am UTC on the 13th).
    lead_actually_yesterday.last_sent_at = datetime(2026, 9, 12, 20, 0)
    store.save(lead_actually_yesterday)

    assert send_batch.leads_sent_today(store, now=now) == 1
