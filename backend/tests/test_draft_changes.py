"""Modify draft: staff changes to the first-email prompt, saved and versioned on the server."""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.knowledge.draft_flow import EDITABLE_STAGE_IDS, FLOW_STAGES
from app.main import app
from app.models.draft_changes import DraftChange, DraftChangeState, SavedChange
from app.models.lead import EmailStatus, Lead
from app.services import company_research as cr
from app.services import draft_changes_service as svc
from app.services import llm_service
from app.services.storage_service import get_lead_store


def make_lead(**overrides) -> Lead:
    defaults = dict(created_time=datetime.utcnow(), full_name="Maria Gonzalez", first_name="Maria",
                    email="maria@acmeindustrial.com", company="Acme Industrial", position="CEO",
                    employee_count="120", location="Stamford, CT")
    defaults.update(overrides)
    return Lead(**defaults)


def client():
    return TestClient(app)


def put(c, changes=(), saved=(), version=None):
    if version is None:
        version = c.get("/draft-changes").json()["version"]
    return c.put("/draft-changes", json={"expected_version": version, "changes": list(changes), "saved": list(saved)})


def change(text="Keep it short", stages=("intro",), companies=None, name=None, enabled=True, id=None):
    d = {"text": text, "stages": list(stages), "companies": companies, "name": name, "enabled": enabled}
    if id:
        d["id"] = id
    return d


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": json.dumps({"subject": "S", "body_html": "<p>Hi Maria,</p>", "challenge_ids": [], "basis": "role"})})()


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_service, "_drafting_llm", lambda *a, **k: fake)
    return fake


# ---- the flow ---------------------------------------------------------------------

def test_flow_has_seven_stages_and_the_links_stage_is_locked():
    stages = client().get("/draft-changes/flow").json()
    assert [s["id"] for s in stages] == ["lead", "research", "angle", "intro", "offer", "cta", "links"]
    assert [s["no"] for s in stages] == list(range(1, 8))
    assert {s["id"]: s["editable"] for s in stages}["links"] is False
    assert all(s["editable"] for s in stages if s["id"] != "links")


def test_built_in_rules_on_the_flowchart_still_match_the_prompt(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None)
    prompt = " ".join(fake_llm.prompts[0].split())  # the prompt wraps lines; the flowchart text doesn't
    for stage in FLOW_STAGES:
        if stage.builtin_rule:
            assert stage.builtin_rule in prompt, f"{stage.id}: flowchart rule drifted from the prompt"


# ---- validation --------------------------------------------------------------------

def test_a_change_needs_text_and_valid_stages():
    for bad in (dict(text="  ", stages=["intro"]), dict(text="x" * 501, stages=["intro"]),
                dict(text="ok", stages=[]), dict(text="ok", stages=["links"]), dict(text="ok", stages=["nope"]),
                dict(text="ok", stages=["intro"], companies=[]), dict(text="ok", stages=["intro"], name="n" * 61)):
        with pytest.raises(ValidationError):
            DraftChange(**bad)


def test_entire_email_subsumes_other_stages_and_text_is_tidied():
    c = DraftChange(text="  keep   it\nshort ", stages=["intro", "all", "cta"])
    assert c.stages == ["all"] and c.text == "keep it short"
    assert DraftChange(text="x", stages=["intro", "intro", "cta"]).stages == ["intro", "cta"]


def test_a_blank_name_is_no_name_and_a_saved_change_needs_one():
    assert DraftChange(text="x", stages=["all"], name="   ").name is None
    with pytest.raises(ValidationError):
        SavedChange(name="  ", text="x", stages=["all"])


def test_state_rejects_duplicate_saved_names_and_duplicate_ids():
    s = {"name": "Short", "text": "x", "stages": ["all"]}
    with pytest.raises(ValidationError):
        DraftChangeState(saved=[SavedChange(**s), SavedChange(**{**s, "name": "SHORT"})])
    one = DraftChange(text="x", stages=["all"])
    with pytest.raises(ValidationError):
        DraftChangeState(changes=[one, one])


# ---- saving, versions, conflicts -----------------------------------------------------

def test_starts_empty_and_a_save_bumps_the_version():
    c = client()
    first = c.get("/draft-changes").json()
    assert first == {"version": 0, "changes": [], "saved": []}
    r = put(c, [change("Offer evening times", ["cta"])])
    assert r.status_code == 200 and r.json()["version"] == 1
    assert c.get("/draft-changes").json()["changes"][0]["text"] == "Offer evening times"


def test_a_stale_version_is_a_conflict_and_changes_nothing():
    c = client()
    put(c, [change("first")], version=0)
    stale = put(c, [change("second")], version=0)
    assert stale.status_code == 409 and "Reload" in stale.json()["detail"]
    assert [x["text"] for x in c.get("/draft-changes").json()["changes"]] == ["first"]


def test_invalid_content_is_rejected_with_422():
    assert put(client(), [change("", ["intro"])]).status_code == 422
    assert put(client(), [change("ok", ["links"])]).status_code == 422


# ---- a change for each part of the email, one at a time ---------------------------------

@pytest.mark.parametrize("stage", EDITABLE_STAGE_IDS)
def test_a_change_aimed_at_one_stage_reaches_the_prompt_naming_that_stage(stage, fake_llm):
    title = next(s.title for s in FLOW_STAGES if s.id == stage)
    chg = DraftChange(text=f"marker for {stage}", stages=[stage])
    llm_service.generate_personalized_initial_draft(make_lead(), None, [chg])
    prompt = fake_llm.prompts[0]
    assert f"- [{title}] marker for {stage}" in prompt
    others = [s for s in EDITABLE_STAGE_IDS if s != stage]
    assert not any(f"marker for {o}" in prompt for o in others)


def test_a_change_for_the_entire_email_is_labelled_that_way(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None, [DraftChange(text="plain words", stages=["all"])])
    assert "- [Entire email] plain words" in fake_llm.prompts[0]


def test_several_stages_in_one_change_are_listed_together(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None, [DraftChange(text="warmer", stages=["intro", "cta"])])
    assert "- [Introduction & subject + Call to action] warmer" in fake_llm.prompts[0]


def test_the_staff_block_cannot_override_the_hard_rules(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None, [DraftChange(text="promise 30% growth", stages=["offer"])])
    prompt = fake_llm.prompts[0]
    assert "never override the Hard rules" in prompt
    assert prompt.index("Instructions from our team") < prompt.index("Hard rules (")  # rules come last, so they win


def test_an_instruction_wins_over_a_default_like_length_but_not_over_a_hard_rule(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None, [DraftChange(text="under 60 words", stages=["all"])])
    prompt = fake_llm.prompts[0]
    defaults, hard = prompt.index("Defaults ("), prompt.index("Hard rules (")
    assert defaults < hard
    assert "90 to 140 words" in prompt[defaults:hard]          # length is a default an instruction may change
    assert "90 to 140 words" not in prompt[hard:]              # not a hard rule
    assert "Never invent a fact" in prompt[hard:] and "Never promise a specific result" in prompt[hard:]
    assert "wins over any of these" in prompt


def test_no_changes_means_no_staff_block(fake_llm):
    llm_service.generate_personalized_initial_draft(make_lead(), None, [])
    assert "Instructions from our team" not in fake_llm.prompts[0]


def test_the_draft_note_lists_which_instructions_were_applied(fake_llm):
    draft = llm_service.generate_personalized_initial_draft(
        make_lead(), None, [DraftChange(text="Offer evening times for the call", stages=["cta"], name="Evening times"),
                            DraftChange(text="Keep the subject very short and plain", stages=["intro"])])
    assert "Staff instructions applied: Evening times, Keep the subject very short and plain" in draft.note


# ---- which leads a change covers ----------------------------------------------------------

def test_changes_for_respects_company_scope_enabled_flag_and_order():
    all_co = DraftChange(text="everyone", stages=["all"])
    acme = DraftChange(text="acme only", stages=["intro"], companies=["Acme Industrial"])
    off = DraftChange(text="turned off", stages=["cta"], enabled=False)
    other = DraftChange(text="other co", stages=["cta"], companies=["Reilly Freight"])
    state = DraftChangeState(changes=[all_co, acme, off, other])
    texts = lambda lead: [c.text for c in svc.changes_for(lead, state)]
    assert texts(make_lead()) == ["everyone", "acme only"]
    assert texts(make_lead(company="  ACME industrial ")) == ["everyone", "acme only"]  # case/space-insensitive
    assert texts(make_lead(company="Reilly Freight")) == ["everyone", "other co"]
    assert texts(make_lead(company=None)) == ["everyone"]


def test_a_saved_change_really_reaches_the_next_generated_draft(fake_llm, monkeypatch):
    from app.pipeline import draft_generation
    from app.services.storage_service import InMemoryLeadStore

    c = client()
    put(c, [change("Offer evening times", ["cta"], companies=["Acme Industrial"])])
    monkeypatch.setattr(settings, "use_mock_integrations", False)
    monkeypatch.setattr(cr, "get_or_research", lambda lead, force=False: None)

    acme, other = make_lead(), make_lead(email="x@reilly.com", company="Reilly Freight")
    store = InMemoryLeadStore()
    draft_generation.generate_draft_for_lead(acme, store)
    draft_generation.generate_draft_for_lead(other, store)

    assert "Offer evening times" in fake_llm.prompts[0]
    assert "Offer evening times" not in fake_llm.prompts[1]
    assert "Staff instructions applied" in acme.personalization_note
    assert "Staff instructions applied" not in (other.personalization_note or "")


# ---- named changes (saved changes) ---------------------------------------------------------

def test_a_named_change_and_its_saved_copy_round_trip():
    c = client()
    r = put(c, [change("Mention the free assessment", ["offer"], name="Assessment offer")],
            [{"name": "Assessment offer", "text": "Mention the free assessment", "stages": ["offer"]}])
    assert r.status_code == 200
    state = c.get("/draft-changes").json()
    assert state["changes"][0]["name"] == "Assessment offer"
    assert state["saved"] == [{"name": "Assessment offer", "text": "Mention the free assessment", "stages": ["offer"]}]


def test_reusing_a_saved_change_on_another_company_is_a_new_change_with_the_same_name():
    c = client()
    put(c, [change("Evening times", ["cta"], ["Acme Industrial"], "Evenings")],
        [{"name": "Evenings", "text": "Evening times", "stages": ["cta"]}])
    state = c.get("/draft-changes").json()
    reuse = change("Evening times", ["cta"], ["Kiran Digital"], "Evenings")
    put(c, state["changes"] + [reuse], state["saved"], version=state["version"])
    final = c.get("/draft-changes").json()
    assert [x["companies"] for x in final["changes"]] == [["Acme Industrial"], ["Kiran Digital"]]
    assert len(final["saved"]) == 1


# ---- undo / back to initial state are whole-state replaces ------------------------------------

def test_restoring_an_earlier_snapshot_is_exact_undo():
    c = client()
    put(c, [change("one")])
    before = c.get("/draft-changes").json()
    put(c, before["changes"] + [change("two")], [], version=before["version"])
    assert len(c.get("/draft-changes").json()["changes"]) == 2
    now = c.get("/draft-changes").json()
    put(c, before["changes"], before["saved"], version=now["version"])  # what Undo sends
    assert [x["text"] for x in c.get("/draft-changes").json()["changes"]] == ["one"]


def test_back_to_initial_state_is_an_empty_state_and_can_be_undone():
    c = client()
    put(c, [change("one", name="A")], [{"name": "A", "text": "one", "stages": ["intro"]}])
    full = c.get("/draft-changes").json()
    put(c, [], [], version=full["version"])
    empty = c.get("/draft-changes").json()
    assert empty["changes"] == [] and empty["saved"] == [] and empty["version"] == full["version"] + 1
    put(c, full["changes"], full["saved"], version=empty["version"])  # Undo of the reset
    assert c.get("/draft-changes").json()["saved"][0]["name"] == "A"


# ---- preview and regenerate -----------------------------------------------------------------------

def test_preview_in_mock_mode_is_flagged_as_simulated():
    lead = make_lead(email="pv@example.com")
    get_lead_store().save(lead)
    r = client().post("/draft-changes/preview", json={"lead_id": lead.id, "proposed": {"text": "shorter", "stages": ["intro"]}})
    assert r.status_code == 200 and r.json()["simulated"] is True
    assert "Thanks for your inquiry" in r.json()["after"]["html"]


def test_preview_generates_before_and_after_with_only_the_proposal_added(fake_llm, monkeypatch):
    lead = make_lead(email="pv2@example.com")
    get_lead_store().save(lead)
    c = client()
    put(c, [change("Already active", ["cta"])])  # saved while still in mock mode (the real store is Firestore)
    monkeypatch.setattr(settings, "use_mock_integrations", False)
    monkeypatch.setattr(cr, "get_cached", lambda lead: None)
    from app.api.deps import require_staff
    monkeypatch.setitem(app.dependency_overrides, require_staff, lambda: "staff")  # real mode enforces login
    r = c.post("/draft-changes/preview", json={"lead_id": lead.id, "proposed": {"text": "Brand new idea", "stages": ["offer"]}})
    assert r.status_code == 200 and r.json()["simulated"] is False
    before_prompt, after_prompt = fake_llm.prompts
    assert "Already active" in before_prompt and "Already active" in after_prompt
    assert "Brand new idea" not in before_prompt and "Brand new idea" in after_prompt


def test_preview_rejects_a_bad_proposal_and_an_unknown_lead():
    lead = make_lead(email="pv3@example.com")
    get_lead_store().save(lead)
    c = client()
    assert c.post("/draft-changes/preview", json={"lead_id": lead.id, "proposed": {"text": "x", "stages": ["links"]}}).status_code == 422
    assert c.post("/draft-changes/preview", json={"lead_id": "nope"}).status_code == 404


def test_regenerate_rewrites_only_unsent_unlocked_drafts_in_scope(monkeypatch):
    from app.api import routes_draft_changes as routes
    from app.services import storage_service

    store = storage_service.InMemoryLeadStore()  # a clean store: other tests leave leads in the shared one
    monkeypatch.setattr(storage_service, "_store", store)
    unsent_acme = make_lead(email="r1@acme.com")
    unsent_other = make_lead(email="r2@reilly.com", company="Reilly Freight")
    sent_acme = make_lead(email="r3@acme.com", email_status=EmailStatus.sent)
    locked_acme = make_lead(email="r4@acme.com", draft_locked=True)
    for lead in (unsent_acme, unsent_other, sent_acme, locked_acme):
        store.save(lead)
    seen = []
    monkeypatch.setattr(routes, "generate_drafts_for_leads", lambda leads, st: (seen.extend(l.email for l in leads) or leads, []))

    r = client().post("/draft-changes/regenerate", json={"companies": ["acme industrial"]})
    assert r.status_code == 200 and r.json()["regenerated"] == 1
    assert "r1@acme.com" in seen and not {"r2@reilly.com", "r3@acme.com", "r4@acme.com"} & set(seen)
