"""
generate_initial_draft is the one "drafting" function that's deliberately
NOT an LLM call (staff instruction: the initial email is a fixed template,
name substituted -- the AI's job starts at the reply). Covered on its own
since it's easy to accidentally reintroduce an LLM call here later without
noticing, given every sibling function in this module does call one.
"""
from datetime import datetime
from unittest.mock import patch

from app.models.lead import Lead
from app.services import llm_service


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Jordan Lee",
        email="jordan@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_initial_draft_is_a_template_not_an_llm_call():
    with patch.object(llm_service, "_drafting_llm") as mock_llm_factory:
        draft = llm_service.generate_initial_draft(make_lead())
        mock_llm_factory.assert_not_called()

    assert "Jordan" in draft.subject
    assert "Hi Jordan," in draft.body_html
    assert "consultation" in draft.body_html.lower()
    assert "reply to this email" in draft.body_html.lower()
    assert "quick call" in draft.body_html.lower()


def test_initial_draft_uses_only_the_first_name():
    draft = llm_service.generate_initial_draft(make_lead(full_name="Casey Ann Nguyen"))
    assert "Hi Casey," in draft.body_html
    assert "Nguyen" not in draft.body_html


def test_initial_draft_handles_a_blank_name_without_crashing():
    draft = llm_service.generate_initial_draft(make_lead(full_name="   "))
    assert "Hi there," in draft.body_html


def test_initial_draft_signs_off_as_the_company_team():
    """
    Every customer-facing email signs off as the same real person -- not
    left to each generation to improvise, so a lead replying anywhere in
    the sequence is replying to a consistent name.
    """
    draft = llm_service.generate_initial_draft(make_lead())
    assert "The Team" in draft.body_html
    assert "VRT Management Group" in draft.body_html
