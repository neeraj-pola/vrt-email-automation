"""
The final automatic follow-up must clearly tell the lead we'll stop
emailing after it -- not just imply it through tone, which an LLM
generation can't be trusted to include reliably every time. A fixed closing
note is appended deterministically rather than left entirely to the model.
"""
import json
from datetime import datetime

from app.config import settings
from app.models.lead import Lead
from app.services import llm_service


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, subject: str, body_html: str):
        self._payload = json.dumps({"subject": subject, "body_html": body_html})

    def invoke(self, prompt: str):
        return _FakeResponse(self._payload)


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_final_follow_up_includes_stop_notice(monkeypatch):
    lead = make_lead(follow_up_count=settings.max_follow_ups - 1)  # about to send the LAST one
    fake = _FakeLLM("Just checking in", "<p>Hope you're doing well.</p>")
    monkeypatch.setattr(llm_service, "_drafting_llm", lambda: fake)

    draft = llm_service.generate_follow_up_draft(lead, "Previous subject", "<p>Previous body</p>")

    assert "won't keep emailing" in draft.body_html
    assert "<p>Hope you're doing well.</p>" in draft.body_html  # model's own content still present


def test_earlier_follow_ups_do_not_include_stop_notice(monkeypatch):
    lead = make_lead(follow_up_count=0)  # about to send follow-up #1, not the last
    fake = _FakeLLM("Just checking in", "<p>Hope you're doing well.</p>")
    monkeypatch.setattr(llm_service, "_drafting_llm", lambda: fake)

    draft = llm_service.generate_follow_up_draft(lead, "Previous subject", "<p>Previous body</p>")

    assert "won't keep emailing" not in draft.body_html
