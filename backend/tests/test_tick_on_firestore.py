"""
The tick on the Firestore store: same outcomes as before, and -- the point of
the redesign -- reads only the leads something happened to. Firestore's free
tier allows 50,000 reads a day; a tick every 5 minutes is 288 a day, so a tick
that read every lead would spend the budget by itself.
"""
from datetime import datetime, timedelta

import pytest

from app.models.lead import AIResponseClassification, EmailStatus, Lead
from app.pipeline.tick import run_pipeline_tick
from app.services import firestore_stores as fs
from app.services import llm_service, schedule_service, storage_service
from app.services.gmail_service import MockGmailClient
from app.services.schedule_service import Schedule
from app.services.storage_service import InMemoryPipelineStateStore
from tests.fake_firestore import FakeFirestore


def lead_(i: int, **kw) -> Lead:
    defaults = dict(created_time=datetime(2026, 10, 1), full_name=f"Lead {i}", first_name="Lead", email=f"lead{i}@x.com",
                    company="Acme Industrial", email_status=EmailStatus.sent, thread_id=f"thread-{i}",
                    last_sent_at=datetime.utcnow() - timedelta(hours=1), current_draft_subject="Following up",
                    current_draft_body_html="<p>Checking in</p>")
    defaults.update(kw)
    return Lead(**defaults)


@pytest.fixture(autouse=True)
def plain_transactions(monkeypatch):
    monkeypatch.setattr(fs, "_transactional", lambda fn: fn)


@pytest.fixture
def db():
    return FakeFirestore()


@pytest.fixture
def store(db, monkeypatch):
    s = fs.FirestoreLeadStore(db)
    monkeypatch.setattr(storage_service, "_store", s)
    return s


@pytest.fixture
def gmail():
    return MockGmailClient()


@pytest.fixture
def state():
    return InMemoryPipelineStateStore()


def test_an_idle_tick_reads_almost_nothing_even_with_hundreds_of_leads_waiting(db, store, gmail, state):
    for i in range(300):
        store.save(lead_(i))
    run_pipeline_tick(gmail, store, state)  # first tick bootstraps the Gmail cursor via the slow path
    db.reads = 0
    outcomes = run_pipeline_tick(gmail, store, state)
    assert outcomes == []
    assert db.reads <= 1  # the "has anyone been emailed" check; nothing is due, nothing arrived


def test_a_reply_costs_a_handful_of_reads_not_a_scan(db, store, gmail, state, monkeypatch):
    for i in range(300):
        store.save(lead_(i))
    run_pipeline_tick(gmail, store, state)
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(classification="unclear"))
    monkeypatch.setattr(llm_service, "generate_unclear_reply_draft", lambda lead, s, b: llm_service.DraftOutput(subject="Quick follow-up", body_html="<p>Hi</p>"))
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, s, b: llm_service.DraftOutput(subject="Next", body_html="<p>Next</p>"))
    db.reads = 0
    gmail.inject_reply("thread-42", "lead42@x.com", "hmm, not sure")
    outcomes = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in outcomes] == ["classified_unclear_resent"]
    assert db.reads < 15  # lookup + hydration + a few saves/queries, nowhere near 300


def test_a_due_follow_up_goes_out_and_is_not_due_again(db, store, gmail, state, monkeypatch):
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, s, b: llm_service.DraftOutput(subject="Next", body_html="<p>Next</p>"))
    due = lead_(1, last_sent_at=datetime.utcnow() - timedelta(days=4))
    waiting = lead_(2, last_sent_at=datetime.utcnow() - timedelta(days=1))
    store.save(due); store.save(waiting)
    outcomes = run_pipeline_tick(gmail, store, state)
    assert [(o.lead_id, o.action) for o in outcomes] == [(due.id, "follow_up_sent")]
    after = store.get(due.id)
    assert after.follow_up_count == 1 and after.last_sent_at > datetime.utcnow() - timedelta(minutes=1)
    assert run_pipeline_tick(gmail, store, state) == []  # its wait restarted


def test_a_company_rule_decides_who_is_due(db, store, gmail, state, monkeypatch):
    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, s, b: llm_service.DraftOutput(subject="Next", body_html="<p>Next</p>"))
    slow = lead_(1, company="Slowpoke Inc", last_sent_at=datetime.utcnow() - timedelta(days=4))
    quick = lead_(2, company="Acme Industrial", last_sent_at=datetime.utcnow() - timedelta(days=4))
    store.save(slow); store.save(quick)
    schedule_service.set_schedule(Schedule(default_days=3, by_company={"Slowpoke Inc": 10}))
    outcomes = run_pipeline_tick(gmail, store, state)
    assert [o.lead_id for o in outcomes] == [quick.id]  # Slowpoke waits 10 days, so it isn't touched


def test_exhaustion_happens_through_the_due_query(db, store, gmail, state):
    lead = lead_(1, follow_up_count=3, last_sent_at=datetime.utcnow() - timedelta(days=4))
    store.save(lead)
    outcomes = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in outcomes] == ["exhausted"] or outcomes[0].action.startswith("exhaust")
    assert store.get(lead.id).email_status == EmailStatus.exhausted
    assert store.list_due_for_follow_up(datetime.utcnow()) == []


def test_a_late_reply_on_a_stopped_lead_is_still_flagged(db, store, gmail, state, monkeypatch):
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(classification="not_interested"))
    stopped = lead_(1, email_status=EmailStatus.unsubscribed)
    store.save(stopped)
    run_pipeline_tick(gmail, store, state)
    gmail.inject_reply("thread-1", "lead1@x.com", "actually, can we talk?")
    outcomes = run_pipeline_tick(gmail, store, state)
    assert [o.action for o in outcomes] == ["late_reply_flagged"]
    assert "actually, can we talk?" in store.get(stopped.id).notes


def test_a_reply_on_a_broken_thread_is_matched_by_sender_and_the_thread_is_repointed(db, store, gmail, state, monkeypatch):
    monkeypatch.setattr(llm_service, "classify_reply", lambda lead, text: llm_service.ClassificationResult(classification="not_interested"))
    store.save(lead_(1))
    run_pipeline_tick(gmail, store, state)
    gmail.inject_reply("brand-new-thread", "LEAD1@x.com", "no thanks")
    run_pipeline_tick(gmail, store, state)
    saved = store.find_by_email("lead1@x.com")
    assert saved.thread_id == "brand-new-thread"
    assert saved.ai_response_classification == AIResponseClassification.not_interested


def test_mail_from_strangers_costs_two_lookups_and_changes_nothing(db, store, gmail, state):
    store.save(lead_(1))
    run_pipeline_tick(gmail, store, state)
    db.reads = 0
    gmail.inject_reply("thread-other", "stranger@elsewhere.com", "buy our SEO services")
    assert run_pipeline_tick(gmail, store, state) == []
    assert db.reads <= 3


def test_nothing_contacted_means_gmail_is_not_polled_at_all(db, store, gmail, state, monkeypatch):
    monkeypatch.setattr(gmail, "poll_new_replies", lambda ids: pytest.fail("polled Gmail with nobody to watch"))
    store.save(lead_(1, email_status=EmailStatus.not_sent, thread_id=None, last_sent_at=None))
    assert run_pipeline_tick(gmail, store, state) == []
