"""
The Firestore stores, run for real against a fake client, and checked against
the in-memory stores so both behave the same. Also checks the things that
matter on Firestore specifically: the derived query fields, read counts, and
that awkward names (slashes, dots) survive.
"""
from datetime import datetime, timedelta

import pytest

from app.models.draft_changes import DraftChange, DraftChangeState
from app.models.lead import AIResponseClassification, DraftVersion, EmailStatus, Lead
from app.services import firestore_stores as fs
from app.services import schedule_service, storage_service
from app.services.company_research import CompanyProfile
from app.services.schedule_service import Schedule
from app.services.storage_service import DraftChangeConflict, InMemoryLeadStore, hydrate_lead
from tests.fake_firestore import FakeFirestore


def make_lead(**overrides) -> Lead:
    defaults = dict(created_time=datetime(2026, 10, 1, 9, 30), full_name="Maria Gonzalez", first_name="Maria",
                    email="maria@acme.com", company="Acme Industrial", position="CEO")
    defaults.update(overrides)
    return Lead(**defaults)


@pytest.fixture(autouse=True)
def plain_transactions(monkeypatch):
    monkeypatch.setattr(fs, "_transactional", lambda fn: fn)  # the fake client has no real transaction machinery


@pytest.fixture
def db():
    return FakeFirestore()


@pytest.fixture(params=["memory", "firestore"])
def store(request, db, monkeypatch):
    s = InMemoryLeadStore() if request.param == "memory" else fs.FirestoreLeadStore(db)
    monkeypatch.setattr(storage_service, "_store", s)  # schedule changes reindex "the" store
    return s


# ---- a lead round-trips unchanged ------------------------------------------------------

def test_a_lead_round_trips_with_every_field_intact(store):
    lead = make_lead(
        thread_id="t-1", last_sent_at=datetime(2026, 10, 2, 14, 5, 6), follow_up_count=2, email_status=EmailStatus.sent,
        ai_response_classification=AIResponseClassification.interested, requested_availability="Weekday afternoons",
        last_reply_text="Yes, let's talk", last_reply_at=datetime(2026, 10, 3, 8, 0),
        website_clicked_at=datetime(2026, 10, 2, 15, 0), scheduled_call_time=datetime(2026, 10, 9, 14, 0),
        notes="called", crm_status="Move to CRM", sale_status="Converted Sale", personalization_note="Framed around: #1",
        current_draft_subject="S", current_draft_body_html="<p>Body</p>", draft_locked=True,
        draft_history=[DraftVersion(subject="old", body_html="<p>old</p>", source="manual", stage="Manual edit", sent=True)],
        linkedin_url="https://linkedin.com/in/x", employee_count="51-200", location="Stamford, CT",
    )
    store.save(lead)
    back = store.get(lead.id)
    assert back.model_dump() == lead.model_dump()
    assert back.last_sent_at.tzinfo is None and back.created_time.tzinfo is None  # naive UTC, as the pipeline expects
    assert back.draft_history[0].body_html == "<p>old</p>"


def test_a_missing_lead_is_none(store):
    assert store.get("nope") is None and store.get_by_thread_id("nope") is None and store.find_by_email("x@y.com") is None


# ---- dedupe on import -------------------------------------------------------------------

def test_upsert_skips_existing_emails_case_insensitively_and_dedupes_within_the_batch(store):
    store.save(make_lead(email="Existing@Acme.com"))
    added = store.upsert_many([make_lead(email="existing@acme.COM"), make_lead(email="a@x.com"), make_lead(email="A@X.com"), make_lead(email="b@x.com")])
    assert added == 2
    assert sorted(l.email.lower() for l in store.list_all()) == ["a@x.com", "b@x.com", "existing@acme.com"]


def test_a_big_import_is_chunked_for_firestore_limits(store):
    leads = [make_lead(email=f"person{i}@co{i % 7}.com") for i in range(450)]  # > 400 per batch, > 30 per `in` query
    assert store.upsert_many(leads) == 450
    assert store.upsert_many(leads) == 0  # re-importing the same sheet adds nothing
    assert len(store.list_all()) == 450


def test_find_by_email_and_thread_lookups(store):
    lead = make_lead(email="Maria@Acme.com", thread_id="t-9")
    store.save(lead)
    assert store.find_by_email("maria@acme.com").id == lead.id
    assert store.find_by_email("  MARIA@ACME.COM ").id == lead.id
    assert store.get_by_thread_id("t-9").id == lead.id
    assert store.email_exists("maria@acme.com") and not store.email_exists("other@acme.com")


# ---- light reads and hydration ------------------------------------------------------------

def test_light_leads_omit_the_heavy_fields_and_hydration_restores_them(store):
    lead = make_lead(current_draft_body_html="<p>Body</p>",
                     draft_history=[DraftVersion(subject="s", body_html="<p>h</p>", source="manual")])
    store.save(lead)
    light = store.list_all_light()[0]
    assert light.current_draft_body_html is None and light.draft_history == []
    hydrate_lead(light, store)
    assert light.current_draft_body_html == "<p>Body</p>" and len(light.draft_history) == 1
    store.save(light)  # saving a hydrated lead must not wipe its history
    assert len(store.get(lead.id).draft_history) == 1


# ---- the queries a tick relies on -----------------------------------------------------------

def _sent(days_ago: float, **kw) -> Lead:
    return make_lead(email=kw.pop("email", f"l{days_ago}@x.com"), email_status=EmailStatus.sent, thread_id=f"t-{days_ago}",
                     last_sent_at=datetime.utcnow() - timedelta(days=days_ago), **kw)


def test_only_leads_past_their_wait_are_due(store):
    old, fresh = _sent(5), _sent(1)
    replied = _sent(9, email="r@x.com", ai_response_classification=AIResponseClassification.interested)
    stopped = make_lead(email="s@x.com", email_status=EmailStatus.unsubscribed, last_sent_at=datetime.utcnow() - timedelta(days=9))
    unsent = make_lead(email="u@x.com")
    for l in (old, fresh, replied, stopped, unsent):
        store.save(l)
    assert [l.id for l in store.list_due_for_follow_up(datetime.utcnow())] == [old.id]


def test_due_leads_come_back_complete_so_they_are_safe_to_save(store):
    lead = _sent(5, current_draft_body_html="<p>next</p>", draft_history=[DraftVersion(subject="s", body_html="<p>h</p>", source="manual")])
    store.save(lead)
    due = store.list_due_for_follow_up(datetime.utcnow())[0]
    assert due.current_draft_body_html == "<p>next</p>" and len(due.draft_history) == 1


def test_changing_the_schedule_changes_who_is_due(store):
    lead = _sent(5)  # sent 5 days ago, default wait is 3 -> due
    store.save(lead)
    assert len(store.list_due_for_follow_up(datetime.utcnow())) == 1
    schedule_service.set_schedule(Schedule(default_days=10))  # now it should wait until day 10
    assert store.list_due_for_follow_up(datetime.utcnow()) == []
    schedule_service.set_schedule(Schedule(default_days=10, by_company={"Acme Industrial": 2}))
    assert len(store.list_due_for_follow_up(datetime.utcnow())) == 1  # a company rule brings it back


def test_a_reply_or_a_stop_removes_a_lead_from_the_due_list(store):
    lead = _sent(5)
    store.save(lead)
    assert len(store.list_due_for_follow_up(datetime.utcnow())) == 1
    lead.ai_response_classification = AIResponseClassification.not_interested
    store.save(lead)
    assert store.list_due_for_follow_up(datetime.utcnow()) == []


def test_contacted_leads_and_the_empty_check(store):
    assert store.has_contacted() is False
    store.save(make_lead(email="a@x.com"))
    assert store.has_contacted() is False  # drafted, never sent
    store.save(_sent(1))
    store.save(make_lead(email="b@x.com", email_status=EmailStatus.bounced, thread_id="t-b"))
    assert store.has_contacted() is True
    assert {l.email for l in store.list_contacted_light()} == {"l1@x.com", "b@x.com"}


def test_count_sent_since_counts_each_lead_once(store):
    since = datetime.utcnow() - timedelta(hours=6)
    store.save(_sent(0.1, email="today1@x.com"))
    store.save(_sent(0.2, email="today2@x.com"))
    store.save(_sent(3, email="old@x.com"))
    store.save(make_lead(email="never@x.com"))
    assert store.count_sent_since(since) == 2


# ---- read budget (Firestore bills per document read) -------------------------------------------

def test_an_idle_tick_query_reads_nothing_while_leads_wait(db):
    s = fs.FirestoreLeadStore(db)
    for i in range(200):
        s.save(_sent(1, email=f"wait{i}@x.com"))
    db.reads = 0
    assert s.list_due_for_follow_up(datetime.utcnow()) == []
    assert db.reads == 0  # 200 leads waiting, none due -> zero documents read


def test_thread_and_sender_lookups_read_one_document(db):
    s = fs.FirestoreLeadStore(db)
    for i in range(50):
        s.save(make_lead(email=f"p{i}@x.com", thread_id=f"t{i}"))
    db.reads = 0
    s.get_by_thread_id("t7"); s.find_by_email("p9@x.com")
    assert db.reads == 2


def test_counting_todays_sends_reads_no_documents(db):
    s = fs.FirestoreLeadStore(db)
    for i in range(30):
        s.save(_sent(0.1, email=f"t{i}@x.com"))
    db.reads = 0
    assert s.count_sent_since(datetime.utcnow() - timedelta(hours=6)) == 30
    assert db.reads == 0


# ---- derived fields are saved on the document ----------------------------------------------------

def test_the_document_carries_the_derived_query_fields(db):
    s = fs.FirestoreLeadStore(db)
    lead = _sent(5, email="Maria@Acme.com")
    s.save(lead)
    doc = db.collection("leads").docs[lead.id]
    assert doc["email_lower"] == "maria@acme.com"
    assert doc["follow_up_due_at"] is not None and doc["follow_up_due_at"].tzinfo is not None
    lead.email_status = EmailStatus.exhausted
    s.save(lead)
    assert db.collection("leads").docs[lead.id]["follow_up_due_at"] is None


# ---- pipeline state ---------------------------------------------------------------------------------

def test_gmail_cursor_compare_and_swap(db):
    s = fs.FirestorePipelineStateStore(db)
    assert s.get_gmail_history_id() is None
    assert s.set_gmail_history_id("100") is True
    assert s.set_gmail_history_id("200", expected_previous="100") is True
    assert s.set_gmail_history_id("300", expected_previous="100") is False  # someone already moved it
    assert s.get_gmail_history_id() == "200"


def test_schedule_round_trips_even_with_awkward_company_names(db):
    s = fs.FirestorePipelineStateStore(db)
    assert s.get_schedule() is None
    names = {"Acme Inc.": 4, "AT&T / Mobility": 5, "__reserved__": 6, "a.b.c": 7}
    s.set_schedule({"default_days": 3, "by_stage": {"2": 7}, "by_company": names})
    back = s.get_schedule()
    assert back == {"default_days": 3, "by_stage": {"2": 7}, "by_company": names}
    assert Schedule(**back).wait_days_for(make_lead(company="at&t / mobility")) == 5


# ---- company profiles ------------------------------------------------------------------------------------

def test_company_profiles_are_keyed_safely_even_when_the_name_has_a_slash(db):
    s = fs.FirestoreCompanyProfileStore(db)
    key = "at&t / mobility"
    assert s.get(key) is None
    s.save(CompanyProfile(key=key, company="AT&T / Mobility", summary="Telecom."))
    assert s.get(key).summary == "Telecom."
    assert s.get("someone else") is None


# ---- Modify draft state ----------------------------------------------------------------------------------

def test_draft_change_state_versions_and_conflicts(db):
    s = fs.FirestoreDraftChangeStore(db)
    assert s.get_state() == DraftChangeState()
    state = DraftChangeState(changes=[DraftChange(text="Offer evening times", stages=["cta"], companies=["AT&T / Mobility"])])
    saved = s.put_state(state, expected_version=0)
    assert saved.version == 1
    again = s.get_state()
    assert again.version == 1 and again.changes[0].companies == ["AT&T / Mobility"]
    with pytest.raises(DraftChangeConflict):
        s.put_state(state, expected_version=0)  # stale
    assert s.put_state(DraftChangeState(), expected_version=1).version == 2
    assert s.get_state().changes == []
