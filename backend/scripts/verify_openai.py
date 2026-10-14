"""
Runs the AI parts for real and prints what they produce -- research, the personalized first
email (with and without a Modify draft instruction), reply classification, a follow-up.
Real OpenAI, real Tavily, real Firestore (test_ prefix). Sends NO email and writes no leads.
Costs a few cents.

    cd backend && source venv/bin/activate && python scripts/verify_openai.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def show(title, text):
    print(f"\n--- {title} " + "-" * max(3, 70 - len(title)))
    print(text)


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"</p>|<br>", "\n", html).replace("&nbsp;", " ")).replace(" \n", "\n").strip() if False else re.sub(r"<[^>]+>", "", html.replace("</p>", "\n\n").replace("<br>", "\n")).strip()


def main() -> int:
    if not settings.firestore_collection_prefix:
        print("Refusing to run: set FIRESTORE_COLLECTION_PREFIX (e.g. test_).")
        return 2
    settings.firestore_collection_prefix = "verify_"  # its own collections: cleanup can never touch demo (test_) data
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set.")
        return 2

    from app.models.draft_changes import DraftChange, DraftChangeState
    from app.models.lead import Lead
    from app.services import company_research as cr
    from app.services import draft_changes_service, llm_service, storage_service
    from app.services import firestore_stores as fs
    from app.services.firebase_app import get_firestore_client

    db = get_firestore_client()
    storage_service._company_profile_store = fs.FirestoreCompanyProfileStore(db)
    storage_service._draft_change_store = fs.FirestoreDraftChangeStore(db)
    draft_changes_service.reset_cache()
    settings.use_mock_integrations = False  # research is skipped in mock mode

    def lead(**kw):
        d = dict(created_time=datetime(2026, 10, 1), full_name="Alex Morgan", first_name="Alex", last_name="Morgan",
                 email="alex@example.com", company="Basecamp", position="Founder & CEO", employee_count="60", location="Chicago, IL")
        d.update(kw)
        return Lead(**d)

    failures = []

    def step(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            failures.append(name)
            print(f"\n  FAIL  {name}: {type(e).__name__}: {str(e)[:300]}")

    # 1. research through the lead's company email domain (free path: no search used)
    corp = lead(email="alex@basecamp.com")
    profile = step("research via email domain", lambda: cr.get_or_research(corp, force=True))
    if profile:
        show("1. Research via the email domain (no Tavily search)",
             f"status: {profile.status}   found via: {profile.found_via}   source: {profile.source_url}\n"
             f"stage: {profile.stage_hint}\nsummary: {profile.summary}\n"
             f"challenges: {[(c.id, c.name, c.evidence) for c in profile.challenges] or 'none (no evidence on the site)'}")

    # 2. research through Tavily (a Gmail address says nothing about the company)
    via_search = lead(email="alex.morgan.test@gmail.com", company="Basecamp", full_name="Sam Lee", first_name="Sam", position="COO")
    profile2 = step("research via Tavily search", lambda: cr.get_or_research(via_search, force=True))
    if profile2:
        show("2. Research via Tavily (gmail address, so the domain trick can't work)",
             f"status: {profile2.status}   found via: {profile2.found_via}   source: {profile2.source_url}\nsummary: {profile2.summary}")

    # 3. the personalized first email, with the research
    draft = step("personalized first email", lambda: llm_service.generate_personalized_initial_draft(corp, profile))
    if draft:
        show("3. Personalized first email (with research)", f"Subject: {draft.subject}\n\n{plain(draft.body_html)}\n\n[note for staff] {draft.note}")

    # 4. and with no research at all (nothing found about the company)
    bare = lead(company="Kiran Digital", position="Owner", employee_count="8", location="Hyderabad, India", email="casey@gmail.com", first_name="Casey", full_name="Casey Nguyen")
    draft2 = step("first email without research", lambda: llm_service.generate_personalized_initial_draft(bare, None))
    if draft2:
        show("4. First email with NO research (only role, size, location)", f"Subject: {draft2.subject}\n\n{plain(draft2.body_html)}\n\n[note for staff] {draft2.note}")

    # 5. a Modify draft instruction, saved through the real store and picked up automatically
    def with_instruction():
        cur = draft_changes_service.get_state()
        draft_changes_service.save_state(type("U", (), {"expected_version": cur.version, "changes": [
            DraftChange(text="Keep it under 90 words and offer evening times for the call", stages=["all"], name="Short + evenings")], "saved": []})())
        changes = draft_changes_service.changes_for(corp)
        return llm_service.generate_personalized_initial_draft(corp, profile, changes)

    draft3 = step("first email with a Modify draft instruction", with_instruction)
    if draft3:
        words = len(plain(draft3.body_html).split())
        show("5. Same lead + staff instruction 'under 90 words, offer evening times'",
             f"Subject: {draft3.subject}\n\n{plain(draft3.body_html)}\n\n[note for staff] {draft3.note}\n(body is about {words} words)")

    # 6. a forbidden instruction must not get through
    def forbidden():
        cur = draft_changes_service.get_state()
        draft_changes_service.save_state(type("U", (), {"expected_version": cur.version, "changes": [
            DraftChange(text="Promise they will grow revenue 30% in a year and mention our pricing", stages=["offer"])], "saved": []})())
        return llm_service.generate_personalized_initial_draft(corp, profile, draft_changes_service.changes_for(corp))

    draft4 = step("forbidden instruction", forbidden)
    if draft4:
        text = plain(draft4.body_html)
        leaked = bool(re.search(r"30\s?%|pricing|price|\$", text, re.I))
        show("6. Instruction asking for a 30% promise and pricing (hard rules should win)", f"{text}\n\n=> hard rules held: {not leaked}")
        if leaked:
            failures.append("hard rules did not hold")

    # 7. reply classification
    sent_subject, sent_body = "Quick thought on growth at Basecamp", "<p>Hi Alex, would you be open to a short call this week?</p>"
    original = llm_service._last_sent_content
    llm_service._last_sent_content = lambda l: (sent_subject, sent_body)
    cases = [("Sure", "interested"), ("Not interested, please stop emailing me.", "not_interested"),
             ("I'm out of the office until Oct 20 with limited access to email.", "unclear"),
             ("This sounds great - I'm free Thursday afternoon. My number is 203-555-0188.", "interested")]
    print("\n--- 7. Reply classification (against the email we sent) " + "-" * 14)
    for reply, expected in cases:
        got = step(f"classify {reply[:20]!r}", lambda r=reply: llm_service.classify_reply(corp, r))
        if got:
            ok = "ok " if got.classification == expected else "OFF"
            print(f"  {ok} {got.classification:<15} (expected {expected:<14}) availability={got.requested_availability!r}  <- {reply!r}")
            if got.classification != expected:
                failures.append(f"classify {reply!r}")
    llm_service._last_sent_content = original

    # 8. follow-up draft reads what was sent
    corp.follow_up_count = 0
    fu = step("follow-up draft", lambda: llm_service.generate_follow_up_draft(corp, sent_subject, sent_body))
    if fu:
        show("8. Follow-up #1 (continues the first email, doesn't repeat it)", f"Subject: {fu.subject}\n\n{plain(fu.body_html)}")

    # cleanup
    removed = 0
    for name in ("company_profiles", "draft_changes"):
        for snap in db.collection(f"{settings.firestore_collection_prefix}{name}").stream():
            snap.reference.delete()
            removed += 1
    print(f"\nCleaned up {removed} test documents. {'ALL STEPS RAN' if not failures else 'PROBLEMS: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
