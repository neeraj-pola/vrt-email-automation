"""
Checks the live pieces together: REAL Firestore + REAL Gmail, with OpenAI stubbed out.
Sends exactly ONE email, to the sender's own address, so nothing reaches anyone else.

    cd backend && source venv/bin/activate && python scripts/verify_live.py

Safety: refuses to run without FIRESTORE_COLLECTION_PREFIX; only touches collections with
that prefix; only ever emails GMAIL_SENDER_ADDRESS; deletes its test data afterwards.
"""
from __future__ import annotations

import re
import sys
import time
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
        return fn
    return wrap


def main() -> int:
    if not settings.firestore_collection_prefix:
        print("Refusing to run: set FIRESTORE_COLLECTION_PREFIX (e.g. test_).")
        return 2
    settings.firestore_collection_prefix = "verify_"  # its own collections: cleanup can never touch demo (test_) data
    me = settings.gmail_sender_address
    if not me:
        print("Refusing to run: GMAIL_SENDER_ADDRESS is not set.")
        return 2

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.lead import AIResponseClassification, EmailStatus, Lead
    from app.pipeline.tick import run_pipeline_tick
    from app.services import firestore_stores as fs
    from app.services import llm_service, storage_service
    from app.services.email_template import render_email_html
    from app.services.firebase_app import get_firestore_client
    from app.services.gmail_service import LiveGmailClient
    from app.services.unsubscribe import build_unsubscribe_url

    prefix = settings.firestore_collection_prefix
    print(f"Sender/recipient: {me}   Firestore prefix: {prefix!r}   public URL: {settings.public_base_url}\n")

    db = get_firestore_client()
    leads = fs.FirestoreLeadStore(db)
    state = fs.FirestorePipelineStateStore(db)
    storage_service._store, storage_service._state_store = leads, state
    gmail = None
    ctx: dict = {}

    @check("gmail: the token loads and the live client starts")
    def _():
        nonlocal gmail
        gmail = LiveGmailClient()
        ctx["history_before"] = gmail.get_history_id()
        assert ctx["history_before"]

    if gmail is None:
        return 1

    lead = Lead(created_time=datetime(2026, 10, 1, 9), full_name="Live Check", first_name="Live", last_name="Check",
                email=me, company="VRT Check Co", position="CEO")
    ctx["lead"] = lead
    leads.save(lead)

    @check("gmail: send one real email (full branded template) to yourself")
    def _():
        body = "<p>Hi Live,</p><p>This is a one-off system check from the VRT lead tool. Nothing else was sent.</p><p>Best regards,<br>The Team<br>VRT Management Group, LLC</p>"
        html = render_email_html(lead, "[TEST] VRT Email Automation check", body)
        assert "Unsubscribe" in html and settings.company_address in html
        sent = gmail.send(me, "[TEST] VRT Email Automation check", html)
        assert sent.message_id and sent.thread_id
        ctx["sent"] = sent

    @check("gmail: the new mail shows up in the mailbox history (the reply-detection path)")
    def _():
        for _ in range(10):
            time.sleep(2)
            new, next_id = gmail.list_new_message_ids_since(ctx["history_before"])
            if any(t == ctx["sent"].thread_id for _m, t in new):
                ctx["found"] = [m for m, t in new if t == ctx["sent"].thread_id]
                return
        raise AssertionError("sent message never appeared in history.list")

    @check("gmail: our own sent mail is NOT treated as a lead's reply")
    def _():
        assert all(gmail.get_message_by_id(m) is None for m in ctx["found"])

    @check("tick (real Gmail + real Firestore): runs cleanly, takes no action on our own mail")
    def _():
        lead.thread_id, lead.email_status, lead.last_sent_at = ctx["sent"].thread_id, EmailStatus.sent, datetime.utcnow()
        leads.save(lead)
        llm_service.classify_reply = lambda l, t: (_ for _ in ()).throw(AssertionError("classified our own mail"))
        outcomes = run_pipeline_tick(gmail, leads, state)
        assert outcomes == [], [o.action for o in outcomes]
        assert state.get_gmail_history_id() is not None, "the Gmail cursor was not saved to Firestore"

    client = TestClient(app)

    @check("unsubscribe: a bad token is refused")
    def _():
        r = client.get("/public/unsubscribe", params={"lead_id": lead.id, "token": "forged"})
        assert r.status_code == 400

    @check("unsubscribe: the real signed link shows a confirmation page and does not unsubscribe on GET")
    def _():
        url = build_unsubscribe_url(lead.id)
        assert url.startswith(settings.public_base_url)
        path = url[len(settings.public_base_url):]
        r = client.get(path)
        assert r.status_code == 200 and "VRT Management Group" in r.text
        assert leads.get(lead.id).email_status == EmailStatus.sent, "a GET must never unsubscribe"
        ctx["path"] = path

    @check("unsubscribe: confirming stops the lead in Firestore, permanently")
    def _():
        r = client.post("/public/unsubscribe", data={"lead_id": lead.id, "token": ctx["path"].split("token=")[1]})
        assert r.status_code == 200, r.text[:200]
        saved = leads.get(lead.id)
        assert saved.email_status == EmailStatus.unsubscribed
        assert leads.list_due_for_follow_up(datetime.utcnow() + timedelta(days=30)) == [], "an unsubscribed lead is still due"

    @check("cleanup: every test document is deleted")
    def _():
        removed = 0
        for name in ("leads", "pipeline_state", "company_profiles", "draft_changes", "healthcheck"):
            for snap in db.collection(f"{prefix}{name}").stream():
                snap.reference.delete()
                removed += 1
        print(f"        removed {removed} test documents")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if not FAILED:
        print(f"One test email is in {me}'s inbox: '[TEST] VRT Email Automation check'. Open it to check how it looks.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
