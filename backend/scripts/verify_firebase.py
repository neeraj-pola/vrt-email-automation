"""
Checks the Firebase setup end to end with REAL Firestore and Firebase Auth but
mock Gmail and mock OpenAI -- so it costs nothing and sends no email.

    cd backend && source venv/bin/activate && python scripts/verify_firebase.py

Safety: it refuses to run unless FIRESTORE_COLLECTION_PREFIX is set (e.g. "test_"),
only ever touches collections with that prefix, and deletes what it created.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

PASSED, FAILED = [], []


def check(name):
    def wrap(fn):
        try:
            fn()
            PASSED.append(name)
            print(f"  ok    {name}")
        except Exception as e:  # noqa: BLE001
            FAILED.append(name)
            print(f"  FAIL  {name}\n        {type(e).__name__}: {str(e)[:300]}")
            if "--trace" in sys.argv:
                traceback.print_exc()
        return fn
    return wrap


def main() -> int:
    if not settings.firestore_collection_prefix:
        print("Refusing to run: set FIRESTORE_COLLECTION_PREFIX (e.g. test_) so real data can't be touched.")
        return 2
    settings.firestore_collection_prefix = "verify_"  # its own collections: cleanup can never touch demo (test_) data

    from app.models.draft_changes import DraftChange, DraftChangeState
    from app.models.lead import AIResponseClassification, DraftVersion, EmailStatus, Lead
    from app.pipeline.tick import run_pipeline_tick
    from app.services import firestore_stores as fs
    from app.services import llm_service, schedule_service, storage_service
    from app.services.company_research import CompanyProfile
    from app.services.firebase_app import get_app, get_firestore_client, staff_allowlist
    from app.services.gmail_service import MockGmailClient
    from app.services.schedule_service import Schedule
    from app.services.storage_service import DraftChangeConflict, hydrate_lead

    prefix = settings.firestore_collection_prefix
    print(f"Project: {settings.firebase_project_id or '(from credentials)'}   collection prefix: {prefix!r}\n")

    db = None

    @check("connect: the service account is accepted and Firestore answers")
    def _():
        nonlocal db
        db = get_firestore_client()
        ref = db.collection(f"{prefix}healthcheck").document("ping")
        ref.set({"ok": True, "at": datetime.utcnow()})
        assert ref.get().to_dict()["ok"] is True
        ref.delete()

    if db is None:
        print("\nCan't continue without a Firestore connection. Most likely causes:")
        print("  - the database hasn't been created yet (Firebase console > Firestore Database > Create database)")
        print("  - FIREBASE_SERVICE_ACCOUNT_FILE / _JSON points at the wrong thing")
        return 1

    leads = fs.FirestoreLeadStore(db)
    state = fs.FirestorePipelineStateStore(db)
    profiles = fs.FirestoreCompanyProfileStore(db)
    changes = fs.FirestoreDraftChangeStore(db)
    # Everything below (schedule reindexing, the tick) reaches storage through these singletons.
    storage_service._store, storage_service._state_store = leads, state
    storage_service._company_profile_store, storage_service._draft_change_store = profiles, changes
    schedule_service.reset_cache()

    def make(i, **kw):
        d = dict(created_time=datetime(2026, 10, 1, 9, 30), full_name=f"Verify Lead{i}", first_name="Verify", last_name=f"Lead{i}",
                 email=f"verify.lead{i}@example.com", company="Verify Co", position="CEO", employee_count="45", location="Stamford, CT")
        d.update(kw)
        return Lead(**d)

    created = []

    @check("leads: save and read back every field, timestamps as naive UTC")
    def _():
        lead = make(1, thread_id="verify-thread-1", last_sent_at=datetime(2026, 10, 2, 14, 5, 6), follow_up_count=1,
                    email_status=EmailStatus.sent, ai_response_classification=AIResponseClassification.interested,
                    last_reply_text="Yes, let's talk", last_reply_at=datetime(2026, 10, 3, 8, 0), notes="n", personalization_note="p",
                    current_draft_subject="S", current_draft_body_html="<p>Body</p>",
                    draft_history=[DraftVersion(subject="old", body_html="<p>old</p>", source="manual", stage="Manual edit", sent=True)])
        created.append(lead.id)
        leads.save(lead)
        back = leads.get(lead.id)
        assert back.model_dump() == lead.model_dump(), "round trip changed the lead"
        assert back.last_sent_at.tzinfo is None

    @check("leads: import dedupe (case-insensitive, within the batch, and against what is stored)")
    def _():
        batch = [make(2), make(2, email="VERIFY.LEAD2@example.com"), make(3), make(1, email="verify.lead1@EXAMPLE.com")]
        created.extend(l.id for l in batch)
        assert leads.upsert_many(batch) == 2
        assert leads.upsert_many(batch) == 0

    @check("leads: lookups by email and by thread")
    def _():
        assert leads.find_by_email("VERIFY.lead3@example.com") is not None
        assert leads.get_by_thread_id("verify-thread-1") is not None
        assert leads.get_by_thread_id("no-such-thread") is None

    @check("leads: light read omits draft bodies, hydration restores them without wiping history")
    def _():
        light = next(l for l in leads.list_all_light() if l.email == "verify.lead1@example.com")
        assert light.current_draft_body_html is None and light.draft_history == []
        hydrate_lead(light, leads)
        assert light.current_draft_body_html == "<p>Body</p>" and len(light.draft_history) == 1
        leads.save(light)
        assert len(leads.get("%s" % light.id).draft_history) == 1

    @check("queries: contacted check, sent-today count, due-for-follow-up")
    def _():
        assert leads.has_contacted() is True
        due_lead = make(4, email_status=EmailStatus.sent, thread_id="verify-thread-4", last_sent_at=datetime.utcnow() - timedelta(days=5),
                        current_draft_subject="Follow-up", current_draft_body_html="<p>Checking in</p>")
        waiting = make(5, email_status=EmailStatus.sent, thread_id="verify-thread-5", last_sent_at=datetime.utcnow() - timedelta(hours=2))
        created.extend([due_lead.id, waiting.id])
        leads.save(due_lead); leads.save(waiting)
        due_ids = {l.id for l in leads.list_due_for_follow_up(datetime.utcnow())}
        assert due_lead.id in due_ids and waiting.id not in due_ids, "due query returned the wrong leads"
        assert leads.count_sent_since(datetime.utcnow() - timedelta(hours=6)) >= 1, "count query found nothing"

    @check("gmail cursor: compare-and-swap in a REAL Firestore transaction")
    def _():
        state.set_gmail_history_id("100")
        assert state.set_gmail_history_id("200", expected_previous="100") is True
        assert state.set_gmail_history_id("300", expected_previous="100") is False
        assert state.get_gmail_history_id() == "200"

    @check("schedule: round trip with awkward company names, and reindexing moves due times")
    def _():
        names = {"Acme Inc.": 4, "AT&T / Mobility": 5, "__reserved__": 6, "a.b.c": 7, "Verify Co": 30}
        schedule_service.set_schedule(Schedule(default_days=3, by_stage={"2": 7}, by_company=names))
        schedule_service.reset_cache()
        back = schedule_service.get_schedule()
        assert back.by_company == names and back.by_stage == {"2": 7}
        ids = {l.id for l in leads.list_due_for_follow_up(datetime.utcnow())}
        assert created[-2] not in ids, "company rule (30 days) should have pushed the 5-day-old lead out of the due list"
        schedule_service.set_schedule(Schedule(default_days=3))  # back to normal for the tick below
        ids = {l.id for l in leads.list_due_for_follow_up(datetime.utcnow())}
        assert created[-2] in ids

    @check("company profiles: a name with a slash round-trips")
    def _():
        key = "verify / co & sons"
        profiles.save(CompanyProfile(key=key, company="Verify / Co & Sons", summary="Makes things."))
        assert profiles.get(key).summary == "Makes things."
        assert profiles.get("nobody") is None

    @check("modify draft: versioned save and a real conflict on a stale version")
    def _():
        current = changes.get_state()
        saved = changes.put_state(DraftChangeState(changes=[DraftChange(text="Offer evening times", stages=["cta"], companies=["AT&T / Mobility"], name="Evenings")]), current.version)
        assert saved.version == current.version + 1
        try:
            changes.put_state(DraftChangeState(), current.version)
        except DraftChangeConflict:
            pass
        else:
            raise AssertionError("a stale write was accepted")
        assert changes.get_state().changes[0].companies == ["AT&T / Mobility"]
        changes.put_state(DraftChangeState(), saved.version)  # leave it empty

    @check("tick (mock Gmail, mock OpenAI): sends the due follow-up, then reads a reply and classifies it")
    def _():
        llm_service.generate_follow_up_draft = lambda lead, s, b: llm_service.DraftOutput(subject="Next", body_html="<p>Next</p>")
        llm_service.classify_reply = lambda lead, text: llm_service.ClassificationResult(classification="not_interested")
        gmail, cursor_state = MockGmailClient(), storage_service.InMemoryPipelineStateStore()
        outcomes = run_pipeline_tick(gmail, leads, cursor_state)
        mine = {o.lead_id: o.action for o in outcomes if o.lead_id in created}
        assert list(mine.values()).count("follow_up_sent") == 1, mine
        sent = leads.get(created[-2])
        assert sent.follow_up_count == 1 and sent.last_sent_at > datetime.utcnow() - timedelta(minutes=2)
        gmail.inject_reply("verify-thread-5", "verify.lead5@example.com", "Not for us, thanks")
        outcomes = run_pipeline_tick(gmail, leads, cursor_state)
        assert any(o.action == "classified_not_interested" for o in outcomes), [o.action for o in outcomes]
        assert leads.get(created[-1]).ai_response_classification == AIResponseClassification.not_interested

    @check("auth: Firebase Auth is on and the staff user exists")
    def _():
        from firebase_admin import auth

        allowed = staff_allowlist()
        assert allowed, "STAFF_EMAIL_ALLOWLIST is empty"
        email = sorted(allowed)[0]
        user = auth.get_user_by_email(email, app=get_app())
        assert not user.disabled, f"{email} is disabled"

    @check("cleanup: every test document is deleted")
    def _():
        removed = 0
        for name in ("leads", "pipeline_state", "company_profiles", "draft_changes", "healthcheck"):
            for snap in db.collection(f"{prefix}{name}").stream():
                snap.reference.delete()
                removed += 1
        print(f"        removed {removed} test documents")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
