"""
The lead-advisor chat (talking points / "what should we say") must work at
any point -- including once the draft is locked (sent) -- unlike the
draft-editing chatbot which requires an unlocked draft. It never touches
current_draft_*.
"""
from datetime import datetime

from app.models.lead import Lead
from app.services import llm_service


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.last_prompt = None

    def invoke(self, prompt: str):
        self.last_prompt = prompt
        return _FakeResponse(self._content)


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
        current_draft_subject="Subject",
        current_draft_body_html="<p>Body</p>",
        draft_locked=True,
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_answer_available_even_when_draft_locked(monkeypatch):
    lead = make_lead(draft_locked=True)
    fake = _FakeLLM("Tell them we can do a virtual walkthrough first.")
    monkeypatch.setattr(llm_service, "_chatbot_llm", lambda: fake)

    answer = llm_service.answer_lead_question(lead, "What if they ask about scheduling a site visit?", [])

    assert answer == "Tell them we can do a virtual walkthrough first."
    # Confirms this never mutates the (locked) draft.
    assert lead.current_draft_subject == "Subject"
    assert lead.current_draft_body_html == "<p>Body</p>"


def test_prior_history_included_in_prompt(monkeypatch):
    lead = make_lead()
    fake = _FakeLLM("Sure, follow up on pricing next.")
    monkeypatch.setattr(llm_service, "_chatbot_llm", lambda: fake)

    llm_service.answer_lead_question(
        lead, "What's next?",
        [{"role": "staff", "text": "They asked about pricing"}, {"role": "assistant", "text": "Mention the free consult"}],
    )

    assert "They asked about pricing" in fake.last_prompt
    assert "Mention the free consult" in fake.last_prompt
