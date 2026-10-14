"""Company research (domain-first, Tavily fallback, cached per company) and the personalized first email."""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.knowledge import loader
from app.main import app
from app.models.lead import Lead
from app.pipeline import draft_generation
from app.services import company_research as cr
from app.services import llm_service
from app.services.storage_service import InMemoryLeadStore, get_lead_store


def make_lead(**overrides) -> Lead:
    defaults = dict(created_time=datetime.utcnow(), full_name="Maria Gonzalez", first_name="Maria",
                    email="maria@acmeindustrial.com", company="Acme Industrial", position="CEO",
                    employee_count="120", location="Stamford, CT")
    defaults.update(overrides)
    return Lead(**defaults)


@pytest.fixture
def live(monkeypatch):
    """Non-mock mode with a fresh profile cache and no real network anywhere."""
    from app.services import storage_service

    monkeypatch.setattr(settings, "use_mock_integrations", False)
    monkeypatch.setattr(storage_service, "_company_profile_store", storage_service.InMemoryCompanyProfileStore())
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")


# ---- knowledge files -------------------------------------------------------

def test_all_27_challenges_parse_in_order():
    items = loader.challenges()
    assert [c.id for c in items] == list(range(1, 28))
    assert items[0].name == "Hiring Quality Staff" and items[26].name == "Limited Capital"
    assert all(c.meaning for c in items)


def test_company_document_covers_the_offer():
    text = loader.vrt_company_text()
    for needle in ("VEGA", "EGOS", "7 Stages", "TriMetrix", "45-minute"):
        assert needle in text


# ---- helpers -----------------------------------------------------------------

def test_free_mail_and_school_domains_are_not_company_domains():
    assert cr.corporate_domain("a@gmail.com") is None
    assert cr.corporate_domain("a@Yahoo.com") is None
    assert cr.corporate_domain("student123@university.ac.in") is None
    assert cr.corporate_domain("jane@acmeindustrial.com") == "acmeindustrial.com"


def test_page_must_mention_the_company():
    assert cr.mentions_company("Acme Industrial LLC", "Acme Industrial | Home", "We make valves")
    assert not cr.mentions_company("Acme Industrial LLC", "Domain for sale", "buy this domain")
    assert not cr.mentions_company("LLC", "anything", "anything")  # nothing distinctive to match on


def test_html_to_text_drops_scripts_and_navigation():
    title, text = cr.html_to_text(
        "<html><head><title>Acme</title><script>var x=1</script></head>"
        "<body><nav>Menu Home</nav><h1>We build valves</h1><p>For plants.</p><footer>(c) 2026</footer></body></html>"
    )
    assert title == "Acme"
    assert "We build valves" in text and "For plants." in text
    assert "var x" not in text and "Menu" not in text and "(c) 2026" not in text


# ---- research order: domain first, search only if needed ------------------------

def _fake_summary(**extra):
    base = {"summary": "Acme makes industrial valves for plants.", "stage_hint": "growing",
            "challenges": [{"id": 1, "evidence": "Careers page lists 14 open roles."}]}
    base.update(extra)
    return lambda company, lead, text: base


def test_corporate_email_domain_is_tried_first_and_search_is_never_called(live, monkeypatch):
    fetched = []
    monkeypatch.setattr(cr, "fetch_page", lambda url: (fetched.append(url), ("Acme Industrial", "Acme Industrial makes valves"))[1])
    monkeypatch.setattr(cr, "tavily_search", lambda q: pytest.fail("search used although the email domain worked"))
    monkeypatch.setattr(cr, "summarize", _fake_summary())

    profile = cr.research_company(make_lead())

    assert fetched[0] == "https://acmeindustrial.com"
    assert profile.status == "ok" and profile.found_via == "email_domain"
    assert profile.challenges[0].id == 1 and profile.challenges[0].name == "Hiring Quality Staff"


def test_gmail_lead_goes_to_search_skipping_social_and_directory_results(live, monkeypatch):
    queries = []

    def search(q):
        queries.append(q)
        return [
            {"url": "https://www.linkedin.com/company/acme", "content": "Acme on LinkedIn"},
            {"url": "https://www.crunchbase.com/organization/acme", "content": "Acme profile"},
            {"url": "https://www.acmeindustrial.com/", "title": "Acme Industrial", "content": "valves"},
        ]

    monkeypatch.setattr(cr, "tavily_search", search)
    monkeypatch.setattr(cr, "fetch_page", lambda url: ("Acme Industrial", "Acme Industrial makes valves") if "acmeindustrial.com" in url else None)
    monkeypatch.setattr(cr, "summarize", _fake_summary())

    profile = cr.research_company(make_lead(email="maria@gmail.com"))

    assert len(queries) == 1 and "Acme Industrial" in queries[0] and "Stamford" in queries[0]
    assert profile.found_via == "search" and "acmeindustrial.com" in profile.website


def test_wrong_site_on_the_email_domain_falls_through_to_search(live, monkeypatch):
    searched = []
    monkeypatch.setattr(cr, "fetch_page", lambda url: ("Domain for sale", "buy this domain today") if "parked.com" in url else ("Acme Industrial", "Acme Industrial valves"))
    monkeypatch.setattr(cr, "tavily_search", lambda q: (searched.append(q), [{"url": "https://acmeindustrial.com", "title": "Acme"}])[1])
    monkeypatch.setattr(cr, "summarize", _fake_summary())

    profile = cr.research_company(make_lead(email="maria@parked.com"))

    assert searched and profile.found_via == "search"


def test_nothing_found_is_a_not_found_profile_not_an_error(live, monkeypatch):
    monkeypatch.setattr(cr, "fetch_page", lambda url: None)
    monkeypatch.setattr(cr, "tavily_search", lambda q: [])
    profile = cr.research_company(make_lead(email="maria@gmail.com"))
    assert profile.status == "not_found" and profile.summary is None and profile.challenges == []


def test_no_tavily_key_means_no_search_attempt(live, monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(cr.httpx, "post", lambda *a, **k: pytest.fail("called Tavily without a key"))
    assert cr.tavily_search("anything") == []


# ---- what the model is allowed to claim ---------------------------------------

def test_challenge_tags_need_a_valid_id_and_real_evidence_and_cap_at_three(live, monkeypatch):
    monkeypatch.setattr(cr, "summarize", _fake_summary(challenges=[
        {"id": 3, "evidence": "Opened 4 new offices this year."},
        {"id": 99, "evidence": "not a real challenge id"},
        {"id": 6, "evidence": ""},                       # no evidence -> dropped
        {"id": 8, "evidence": "Rapid expansion."},
        {"id": 9, "evidence": "Unclear growth plan."},
        {"id": 11, "evidence": "fifth one, over the cap"},
    ]))
    profile = cr._build_profile("Acme Industrial", make_lead(), ("t", "text", "https://acmeindustrial.com"), "email_domain")
    # 99 (not a real id) and 6 (no evidence) are dropped without using up slots; the cap is 3 valid tags
    assert [c.id for c in profile.challenges] == [3, 8, 9]


def test_a_summary_that_comes_back_empty_is_treated_as_not_found(live, monkeypatch):
    monkeypatch.setattr(cr, "summarize", _fake_summary(summary="", challenges=[]))
    profile = cr._build_profile("Acme Industrial", make_lead(), ("t", "text", "https://x.com"), "search")
    assert profile.status == "not_found"


# ---- caching: one run per company ----------------------------------------------

def test_one_research_run_serves_every_lead_at_the_company_including_a_cached_miss(live, monkeypatch):
    runs = []

    def research(lead):
        runs.append(lead.email)
        return cr.CompanyProfile(key=cr.company_key(lead.company), company=lead.company, status="ok", summary="s")

    monkeypatch.setattr(cr, "research_company", research)

    a = cr.get_or_research(make_lead(email="a@acmeindustrial.com", company="Acme Industrial"))
    b = cr.get_or_research(make_lead(email="b@acmeindustrial.com", company="  ACME industrial "))
    assert len(runs) == 1 and a.summary == b.summary

    cr.get_or_research(make_lead(company="Acme Industrial"), force=True)
    assert len(runs) == 2  # Refresh re-runs

    cr.get_or_research(make_lead(company="Other Co"))
    assert len(runs) == 3  # different company -> its own run


def test_not_found_is_cached_so_a_dead_company_is_not_searched_every_draft(live, monkeypatch):
    runs = []
    monkeypatch.setattr(cr, "research_company", lambda lead: (runs.append(1), cr.CompanyProfile(key="x", company="X", status="not_found"))[1])
    cr.get_or_research(make_lead(company="X"))
    cr.get_or_research(make_lead(company="X"))
    assert len(runs) == 1


def test_no_company_or_mock_mode_means_no_research(monkeypatch):
    monkeypatch.setattr(cr, "research_company", lambda lead: pytest.fail("researched"))
    assert cr.get_or_research(make_lead(company=None)) is None
    assert cr.get_or_research(make_lead()) is None  # default test settings = mock mode


# ---- the personalized email ------------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": json.dumps(self.payload)})()


def test_personalized_draft_uses_role_company_and_evidence_and_appends_the_signoff(monkeypatch):
    fake = _FakeLLM({"subject": "Hiring ahead of growth at Acme", "body_html": "<p>Hi Maria,</p><p>We can help.</p>",
                     "challenge_ids": [1, 99, 1], "basis": "their open roles page"})
    monkeypatch.setattr(llm_service, "_drafting_llm", lambda *a, **k: fake)
    profile = cr.CompanyProfile(key="acme industrial", company="Acme Industrial", status="ok",
                                source_url="https://acmeindustrial.com", summary="Acme makes valves.",
                                stage_hint="growing",
                                challenges=[cr.ChallengeMatch(id=1, name="Hiring Quality Staff", evidence="14 open roles")])

    draft = llm_service.generate_personalized_initial_draft(make_lead(), profile)

    prompt = fake.prompts[0]
    for needle in ("Maria", "CEO", "Acme Industrial", "120", "Stamford", "Acme makes valves.", "14 open roles",
                   "Hiring Quality Staff", "Limited Capital", "VEGA", "Do NOT state or imply that"):
        assert needle in prompt, needle
    assert draft.subject == "Hiring ahead of growth at Acme"
    assert draft.body_html.endswith("The Team<br>VRT Management Group, LLC</p>")
    assert draft.note == "Framed around: #1 Hiring Quality Staff. Based on: their open roles page"  # id 99 + duplicate dropped


def test_without_research_the_prompt_forbids_claims_about_the_company(monkeypatch):
    fake = _FakeLLM({"subject": "s", "body_html": "<p>Hi</p>", "challenge_ids": [], "basis": "their role and company size"})
    monkeypatch.setattr(llm_service, "_drafting_llm", lambda *a, **k: fake)
    draft = llm_service.generate_personalized_initial_draft(make_lead(), None)
    assert "do not describe what the company does" in fake.prompts[0]
    assert draft.note == "Based on: their role and company size"


# ---- draft generation: personalized with a safe fallback ----------------------------

def test_mock_mode_still_uses_the_fixed_template():
    store = InMemoryLeadStore()
    lead = make_lead()
    draft_generation.generate_draft_for_lead(lead, store)
    assert "Thanks for your inquiry" in lead.current_draft_body_html and lead.personalization_note is None


def test_generating_a_draft_researches_then_personalizes(live, monkeypatch):
    store = InMemoryLeadStore()
    seen = {}
    monkeypatch.setattr(cr, "get_or_research", lambda lead, force=False: seen.setdefault("p", "PROFILE"))
    monkeypatch.setattr(llm_service, "generate_personalized_initial_draft", lambda lead, profile, *a, **k: llm_service.PersonalizedDraft(
        subject="Hello Acme", body_html="<p>Hi Maria,</p>", note="Framed around: #3 No Systems in Place. Based on: their size"))
    lead = make_lead()
    draft_generation.generate_draft_for_lead(lead, store)
    assert lead.current_draft_subject == "Hello Acme"
    assert lead.personalization_note.startswith("Framed around: #3")


def test_a_failed_personalization_falls_back_to_the_template_and_says_so(live, monkeypatch):
    store = InMemoryLeadStore()
    monkeypatch.setattr(cr, "get_or_research", lambda lead, force=False: None)
    monkeypatch.setattr(llm_service, "generate_personalized_initial_draft", lambda lead, profile, *a, **k: (_ for _ in ()).throw(RuntimeError("openai down")))
    lead = make_lead()
    draft_generation.generate_draft_for_lead(lead, store)
    assert "Thanks for your inquiry" in lead.current_draft_body_html
    assert "template" in lead.personalization_note


def test_a_research_crash_does_not_block_the_personalized_draft(live, monkeypatch):
    store = InMemoryLeadStore()
    monkeypatch.setattr(cr, "get_or_research", lambda lead, force=False: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(llm_service, "generate_personalized_initial_draft", lambda lead, profile, *a, **k: llm_service.PersonalizedDraft(
        subject="s", body_html="<p>b</p>", note="Based on: their role"))
    lead = make_lead()
    draft_generation.generate_draft_for_lead(lead, store)
    assert lead.current_draft_subject == "s"


def test_batch_generation_returns_leads_in_input_order_and_reports_failures(monkeypatch):
    store = InMemoryLeadStore()
    leads = [make_lead(email=f"l{i}@example.com", first_name=f"N{i}", full_name=f"N{i} X") for i in range(6)]
    leads[2].draft_locked = True  # will raise
    updated, errors = draft_generation.generate_drafts_for_leads(leads, store)
    assert [l.email for l in updated] == [f"l{i}@example.com" for i in (0, 1, 3, 4, 5)]
    assert len(errors) == 1 and errors[0]["lead_id"] == leads[2].id


# ---- API -------------------------------------------------------------------------------

def test_company_profile_endpoint_returns_null_before_any_research():
    lead = make_lead(email="api@example.com")
    get_lead_store().save(lead)
    resp = TestClient(app).get(f"/leads/{lead.id}/company-profile")
    assert resp.status_code == 200 and resp.json() is None
    assert TestClient(app).post(f"/leads/{lead.id}/company-profile/refresh").status_code == 200
    bare = make_lead(email="nocompany@example.com", company=None)
    get_lead_store().save(bare)
    assert TestClient(app).post(f"/leads/{bare.id}/company-profile/refresh").status_code == 409


def test_research_that_does_not_look_like_the_same_company_is_discarded(live, monkeypatch):
    """A name can match several businesses (a project-management app vs a gym with the same name)."""
    monkeypatch.setattr(cr, "summarize", _fake_summary(same_company=False))
    profile = cr._build_profile("Basecamp", make_lead(company="Basecamp"), ("t", "text", "https://basecampfitness.com"), "search")
    assert profile.status == "not_found" and profile.summary is None and profile.challenges == []


def test_the_model_is_asked_whether_it_is_the_same_company(live, monkeypatch):
    class Fake:
        prompts = []
        def invoke(self, prompt):
            Fake.prompts.append(prompt)
            return type("R", (), {"content": json.dumps({"summary": "s", "stage_hint": "growing", "same_company": True, "challenges": []})})()
    monkeypatch.setattr(llm_service, "_classification_llm", lambda: Fake())
    cr.summarize("Basecamp", make_lead(position="Founder & CEO", employee_count="60", location="Chicago, IL"), "page text")
    prompt = Fake.prompts[0]
    assert '"same_company"' in prompt and "Founder & CEO" in prompt and "60" in prompt and "Chicago, IL" in prompt
    assert "a wrong company is worse than none" in prompt


def test_map_listings_and_social_pages_are_never_used_as_a_company_website():
    for url in ("https://maps.apple.com/place?place-id=X", "https://maps.google.com/?q=acme", "https://www.tripadvisor.com/Restaurant-acme",
                "https://www.yellowpages.com/acme", "https://www.reddit.com/r/acme", "https://www.linkedin.com/company/acme"):
        assert not cr._usable_result({"url": url}), url
    assert cr._usable_result({"url": "https://www.acmeindustrial.com/about"})
