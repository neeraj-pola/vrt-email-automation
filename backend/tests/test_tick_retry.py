"""
A failed tick must not lose mail. The position in Gmail's history is saved only after the replies it
covers were handled; a failure leaves it where it was so the next tick sees the same mail again, and
handling is idempotent so nothing is done twice.
"""
import logging
from datetime import datetime, timedelta

import pytest

from app.models.lead import AIResponseClassification, EmailStatus, Lead
from app.pipeline import tick as tick_module
from app.pipeline.tick import run_pipeline_tick
from app.services import llm_service
from app.services.gmail_service import MockGmailClient, begin_poll, poll_new_replies_with_history_cursor
from app.services.storage_service import InMemoryLeadStore, InMemoryPipelineStateStore


def lead_(i, **kw) -> Lead:
    d = dict(created_time=datetime(2026, 10, 1), full_name=f"Lead {i}", first_name="Lead", email=f"lead{i}@x.com",
             company="Acme", email_status=EmailStatus.sent, thread_id=f"thread-{i}",
             last_sent_at=datetime.utcnow() - timedelta(hours=1), current_draft_subject="Follow-up",
             current_draft_body_html="<p>Checking in</p>")
    d.update(kw)
    return Lead(**d)


@pytest.fixture(autouse=True)
def reset_failure_streak(monkeypatch):
    monkeypatch.setattr(tick_module, "_failed_ticks", 0)


@pytest.fixture
def store():
    return InMemoryLeadStore()


@pytest.fixture
def gmail():
    return MockGmailClient()


@pytest.fixture
def state():
    return InMemoryPipelineStateStore()


def boot(store, gmail, state, *leads):
    """Saves the leads and runs one quiet tick so the history position exists (the steady state)."""
    for l in leads:
        store.save(l)
    run_pipeline_tick(gmail, store, state)
    return state.get_gmail_history_id()


def classify_as(monkeypatch, result="interested", fail_for=()):
    from app.config import settings

    monkeypatch.setattr(settings, "company_notification_email", "staff@example.com")  # staff are emailed about interested leads
    calls = []

    def fake(lead, text):
        calls.append(lead.email)
        if lead.email in fail_for:
            raise RuntimeError("OpenAI is down")
        return llm_service.ClassificationResult(classification=result)

    monkeypatch.setattr(llm_service, "classify_reply", fake)
    monkeypatch.setattr(llm_service, "generate_company_notification_email",
                        lambda l, r, t: llm_service.DraftOutput(subject="Notify", body_html="<p>x</p>"))
    return calls


# ---- the position is saved only after success --------------------------------------------------

def test_a_failed_classification_leaves_the_gmail_position_where_it_was_and_the_reply_is_retried(store, gmail, state, monkeypatch):
    lead = lead_(1)
    before = boot(store, gmail, state, lead)
    classify_as(monkeypatch, fail_for={"lead1@x.com"})
    gmail.inject_reply("thread-1", "lead1@x.com", "Yes, interested")

    out = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in out] == ["error"]
    assert state.get_gmail_history_id() == before                       # NOT advanced past the reply
    assert store.get(lead.id).ai_response_classification is None        # nothing half-saved

    classify_as(monkeypatch)                                            # OpenAI is back
    out = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in out] == ["classified_interested_notified"]
    assert store.get(lead.id).ai_response_classification == AIResponseClassification.interested
    assert state.get_gmail_history_id() != before                       # now it moves on


def test_a_successful_tick_saves_the_position(store, gmail, state, monkeypatch):
    before = boot(store, gmail, state, lead_(1))
    classify_as(monkeypatch)
    gmail.inject_reply("thread-1", "lead1@x.com", "Yes")
    run_pipeline_tick(gmail, store, state)
    assert state.get_gmail_history_id() != before


def test_one_leads_failure_does_not_stop_the_others_and_nothing_is_done_twice_on_retry(store, gmail, state, monkeypatch):
    a, b = lead_(1), lead_(2)
    before = boot(store, gmail, state, a, b)
    calls = classify_as(monkeypatch, fail_for={"lead1@x.com"})
    gmail.inject_reply("thread-1", "lead1@x.com", "Yes")
    gmail.inject_reply("thread-2", "lead2@x.com", "Count me in")

    out = run_pipeline_tick(gmail, store, state)
    assert sorted(o.action for o in out) == ["classified_interested_notified", "error"]
    assert store.get(b.id).ai_response_classification == AIResponseClassification.interested   # B went through
    assert state.get_gmail_history_id() == before                                              # but A holds the position back
    notifications_after_first = len(gmail._sent)

    classify_as(monkeypatch)                                                                   # A can now succeed
    out = run_pipeline_tick(gmail, store, state)
    actions = sorted(o.action for o in out)
    assert actions == ["classified_interested_notified"]                  # only A; B is already done and skipped
    assert len(gmail._sent) == notifications_after_first + 1             # exactly one new notification, none repeated for B
    assert state.get_gmail_history_id() != before


def test_retrying_never_resends_to_a_lead_whose_reply_was_already_answered(store, gmail, state, monkeypatch):
    """An 'unclear' reply gets an automatic reply. If another lead fails in the same tick, the retry must not answer it again."""
    a, b = lead_(1), lead_(2)
    boot(store, gmail, state, a, b)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: (_ for _ in ()).throw(RuntimeError("down")) if lead.email == "lead2@x.com"
                        else llm_service.ClassificationResult(classification="unclear"))
    monkeypatch.setattr(llm_service, "generate_unclear_reply_draft", lambda l, s, b_: llm_service.DraftOutput(subject="Quick follow-up", body_html="<p>Hi</p>"))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda l, s, b_: llm_service.DraftOutput(subject="Next", body_html="<p>n</p>"))
    gmail.inject_reply("thread-1", "lead1@x.com", "hmm")
    gmail.inject_reply("thread-2", "lead2@x.com", "yes")

    run_pipeline_tick(gmail, store, state)
    sent_after_first = len(gmail._sent)            # lead 1 got its automatic reply
    assert sent_after_first == 1
    run_pipeline_tick(gmail, store, state)          # lead 2 still failing: the same mail is re-read
    run_pipeline_tick(gmail, store, state)
    assert len(gmail._sent) == sent_after_first    # lead 1 was NOT answered again


def test_a_stuck_message_is_retried_a_few_times_then_the_position_moves_on(store, gmail, state, monkeypatch, caplog):
    before = boot(store, gmail, state, lead_(1), lead_(2))
    classify_as(monkeypatch, fail_for={"lead1@x.com"})
    gmail.inject_reply("thread-1", "lead1@x.com", "Yes")

    for attempt in range(1, tick_module._MAX_FAILED_TICKS):
        run_pipeline_tick(gmail, store, state)
        assert state.get_gmail_history_id() == before, f"attempt {attempt}: should still be retrying"

    with caplog.at_level(logging.ERROR):
        run_pipeline_tick(gmail, store, state)     # the last allowed attempt
    assert state.get_gmail_history_id() != before  # gave up on that message so later mail isn't blocked
    assert any("manual look" in r.message for r in caplog.records)

    gmail.inject_reply("thread-2", "lead2@x.com", "Hello")   # a later, healthy reply still gets through
    classify_as(monkeypatch)
    out = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in out] == ["classified_interested_notified"]


def test_the_failure_streak_resets_after_a_good_tick(store, gmail, state, monkeypatch):
    boot(store, gmail, state, lead_(1))
    classify_as(monkeypatch, fail_for={"lead1@x.com"})
    gmail.inject_reply("thread-1", "lead1@x.com", "Yes")
    run_pipeline_tick(gmail, store, state)
    assert tick_module._failed_ticks == 1
    classify_as(monkeypatch)
    run_pipeline_tick(gmail, store, state)
    assert tick_module._failed_ticks == 0


def test_a_failing_follow_up_send_is_retried_and_holds_the_position_back(store, gmail, state, monkeypatch):
    due = lead_(1, last_sent_at=datetime.utcnow() - timedelta(days=5))
    before = boot(store, gmail, state)            # bootstrap with nobody due yet
    store.save(due)
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda l, s, b: llm_service.DraftOutput(subject="Next", body_html="<p>n</p>"))
    real_send = gmail.send
    monkeypatch.setattr(gmail, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Gmail is unreachable")))
    out = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in out] == ["error"]
    assert tick_module._failed_ticks == 1
    assert store.get(due.id).follow_up_count == 0          # nothing recorded for a send that never happened

    monkeypatch.setattr(gmail, "send", real_send)
    out = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in out] == ["follow_up_sent"]
    assert store.get(due.id).follow_up_count == 1


# ---- bounce notices are not replies ------------------------------------------------------------------

def test_a_bounce_notice_seen_again_on_retry_is_not_flagged_as_a_late_reply(store, gmail, state, monkeypatch):
    bouncer, other = lead_(1), lead_(2)
    boot(store, gmail, state, bouncer, other)
    classify_as(monkeypatch, fail_for={"lead2@x.com"})
    gmail.inject_reply("thread-1", "mailer-daemon@googlemail.com", "Delivery failed")
    gmail.inject_reply("thread-2", "lead2@x.com", "Yes")

    out1 = run_pipeline_tick(gmail, store, state)
    assert any(o.action == "bounced" for o in out1)
    assert store.get(bouncer.id).email_status == EmailStatus.bounced
    out2 = run_pipeline_tick(gmail, store, state)                          # the same bounce notice is read again
    assert all(o.lead_id != bouncer.id for o in out2)                     # ignored, not 'late_reply_flagged'
    assert not store.get(bouncer.id).notes


# ---- the polling itself --------------------------------------------------------------------------------

def test_begin_poll_does_not_save_the_position_until_commit_on_the_fast_path(gmail, state):
    state.set_gmail_history_id(gmail.get_history_id())
    start = state.get_gmail_history_id()
    gmail.inject_reply("thread-1", "a@x.com", "hi")
    poll = begin_poll(gmail, state, {"thread-1"})
    assert [r.message_id for r in poll.replies] and state.get_gmail_history_id() == start
    poll.commit()
    assert state.get_gmail_history_id() != start


def test_begin_poll_does_not_save_the_position_until_commit_on_the_first_run(gmail, state):
    gmail.inject_reply("thread-1", "a@x.com", "hi")
    poll = begin_poll(gmail, state, {"thread-1"})
    assert state.get_gmail_history_id() is None and len(poll.replies) == 1
    poll.commit()
    assert state.get_gmail_history_id() is not None


def test_the_old_one_call_helper_still_saves_the_position_immediately(gmail, state):
    gmail.inject_reply("thread-1", "a@x.com", "hi")
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})
    assert state.get_gmail_history_id() is not None


def test_mail_arriving_during_the_first_full_poll_is_not_lost(gmail, state, monkeypatch):
    """The position is read before polling, so a message that lands mid-poll shows up on the next poll."""
    real = gmail.poll_new_replies

    def poll_and_receive_mail(thread_ids):
        out = real(thread_ids)
        gmail.inject_reply("thread-9", "late@x.com", "arrived while we were polling")
        return out

    monkeypatch.setattr(gmail, "poll_new_replies", poll_and_receive_mail)
    first = begin_poll(gmail, state, {"thread-1"})
    first.commit()
    second = begin_poll(gmail, state, {"thread-1"})
    assert [r.from_email for r in second.replies] == ["late@x.com"]
