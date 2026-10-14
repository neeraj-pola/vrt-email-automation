"""
Regression coverage: a chatbot rewrite used to overwrite the draft with no
way back to the previous version. Now every overwrite snapshots into
lead.draft_history first.
"""
from datetime import datetime, timedelta

from app.models.lead import DraftVersionSource, Lead
from app.pipeline import draft_generation
from app.services import llm_service
from app.services.storage_service import InMemoryLeadStore


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_chatbot_edit_preserves_previous_version(monkeypatch):
    store = InMemoryLeadStore()
    lead = make_lead(current_draft_subject="V1 subject", current_draft_body_html="<p>V1 body</p>")
    store.save(lead)

    monkeypatch.setattr(llm_service, "apply_chatbot_modification", lambda draft, instruction: llm_service.DraftOutput(
        subject="V2 subject", body_html="<p>V2 body</p>"
    ))

    updated = draft_generation.apply_chatbot_edit(lead, "make it shorter", store)

    assert updated.current_draft_subject == "V2 subject"
    assert len(updated.draft_history) == 1
    assert updated.draft_history[0].subject == "V1 subject"
    assert updated.draft_history[0].body_html == "<p>V1 body</p>"
    assert updated.draft_history[0].source == DraftVersionSource.chatbot


def test_multiple_edits_accumulate_history_in_order(monkeypatch):
    store = InMemoryLeadStore()
    lead = make_lead(current_draft_subject="V1", current_draft_body_html="<p>V1</p>")
    store.save(lead)

    monkeypatch.setattr(llm_service, "apply_chatbot_modification", lambda draft, instruction: llm_service.DraftOutput(
        subject="V2", body_html="<p>V2</p>"
    ))
    draft_generation.apply_chatbot_edit(lead, "change 1", store)

    monkeypatch.setattr(llm_service, "apply_chatbot_modification", lambda draft, instruction: llm_service.DraftOutput(
        subject="V3", body_html="<p>V3</p>"
    ))
    draft_generation.apply_chatbot_edit(lead, "change 2", store)

    assert [v.subject for v in lead.draft_history] == ["V1", "V2"]
    assert lead.current_draft_subject == "V3"


def test_restore_version_makes_it_current_and_snapshots_what_it_replaced():
    store = InMemoryLeadStore()
    lead = make_lead(current_draft_subject="V2", current_draft_body_html="<p>V2</p>")
    from app.models.lead import DraftVersion
    lead.draft_history = [DraftVersion(subject="V1", body_html="<p>V1</p>", source=DraftVersionSource.initial)]
    store.save(lead)

    restored = draft_generation.restore_draft_version(lead, 0, store)

    assert restored.current_draft_subject == "V1"
    assert restored.current_draft_body_html == "<p>V1</p>"
    # V2 (what restore replaced) is preserved too, not lost.
    assert any(v.subject == "V2" for v in restored.draft_history)


def test_manual_edit_also_snapshots():
    store = InMemoryLeadStore()
    lead = make_lead(current_draft_subject="V1", current_draft_body_html="<p>V1</p>")
    store.save(lead)

    draft_generation.apply_manual_edit(lead, "V2", "<p>V2</p>", store)

    assert lead.current_draft_subject == "V2"
    assert len(lead.draft_history) == 1
    assert lead.draft_history[0].subject == "V1"


def test_automatic_follow_up_snapshots_previous_stage_into_history(monkeypatch):
    """
    Regression test: the automatic pipeline (send_follow_up_now) used to
    overwrite current_draft_* with the follow-up content with no snapshot at
    all -- so once a follow-up went out, the initial email's content was
    just gone, and the version dropdown showed nothing.
    """
    from app.pipeline import outreach_graph
    from app.services.gmail_service import MockGmailClient

    store = InMemoryLeadStore()
    gmail = MockGmailClient()
    lead = make_lead(
        email_status="sent", thread_id="t1", last_sent_at=datetime.utcnow() - timedelta(days=3),
        current_draft_subject="Initial subject", current_draft_body_html="<p>Initial body</p>",
        draft_locked=True, follow_up_count=0,
    )
    store.save(lead)

    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Follow-up subject", body_html="<p>Follow-up body</p>"
    ))
    outreach_graph.send_follow_up_now(lead, gmail, store)

    assert lead.current_draft_subject == "Follow-up subject"
    assert len(lead.draft_history) == 1
    assert lead.draft_history[0].subject == "Initial subject"
    assert lead.draft_history[0].stage == "Initial outreach"
    assert lead.draft_history[0].source == DraftVersionSource.pipeline
    assert lead.draft_history[0].sent is True


def test_follow_up_prompt_receives_previous_email_content(monkeypatch):
    """The follow-up prompt must actually see what was sent before, not write blind."""
    from app.pipeline import outreach_graph
    from app.services.gmail_service import MockGmailClient

    store = InMemoryLeadStore()
    gmail = MockGmailClient()
    lead = make_lead(
        email_status="sent", thread_id="t2", last_sent_at=datetime.utcnow() - timedelta(days=3),
        current_draft_subject="Let's talk growth", current_draft_body_html="<p>A very specific unique sentence.</p>",
        draft_locked=True, follow_up_count=0,
    )
    store.save(lead)

    captured = {}

    def fake_generate(lead, prev_subject, prev_body):
        captured["prev_subject"] = prev_subject
        captured["prev_body"] = prev_body
        return llm_service.DraftOutput(subject="Follow-up", body_html="<p>Following up</p>")

    monkeypatch.setattr(llm_service, "generate_follow_up_draft", fake_generate)
    outreach_graph.send_follow_up_now(lead, gmail, store)

    assert captured["prev_subject"] == "Let's talk growth"
    assert "A very specific unique sentence." in captured["prev_body"]


def test_second_follow_up_snapshots_first_follow_up_with_correct_stage_label(monkeypatch):
    from app.pipeline import outreach_graph
    from app.services.gmail_service import MockGmailClient

    store = InMemoryLeadStore()
    gmail = MockGmailClient()
    lead = make_lead(
        email_status="sent", thread_id="t3", last_sent_at=datetime.utcnow() - timedelta(days=3),
        current_draft_subject="Follow-up 1 subject", current_draft_body_html="<p>Follow-up 1 body</p>",
        draft_locked=True, follow_up_count=1,  # already sent one follow-up
    )
    store.save(lead)

    monkeypatch.setattr(llm_service, "generate_follow_up_draft", lambda lead, prev_subject, prev_body: llm_service.DraftOutput(
        subject="Follow-up 2 subject", body_html="<p>Follow-up 2 body</p>"
    ))
    outreach_graph.send_follow_up_now(lead, gmail, store)

    assert lead.follow_up_count == 2
    assert lead.draft_history[0].stage == "Follow-up 1"


def test_first_generation_does_not_create_an_empty_history_entry(monkeypatch):
    store = InMemoryLeadStore()
    lead = make_lead()  # no draft yet
    store.save(lead)

    monkeypatch.setattr(llm_service, "generate_initial_draft", lambda lead: llm_service.DraftOutput(
        subject="V1", body_html="<p>V1</p>"
    ))
    draft_generation.generate_draft_for_lead(lead, store)

    assert lead.draft_history == []
