"""
The stages of the first-email pipeline, as shown on the Modify draft page's
flowchart. One source of truth: the page renders this list, and staff
instructions are aimed at these ids, so the prompt can name the exact part
of the email an instruction is about.

`builtin_rule` mirrors the matching rule in the prompt
(llm_service.generate_personalized_initial_draft) -- keep them in step; a
test checks the prompt still contains each one.
"""
from __future__ import annotations

from dataclasses import dataclass

ALL_STAGES = "all"  # an instruction aimed at the whole email


@dataclass(frozen=True)
class FlowStage:
    id: str
    no: int
    title: str
    description: str
    chips: tuple[str, ...]
    builtin_rule: str | None
    editable: bool = True


FLOW_STAGES: tuple[FlowStage, ...] = (
    FlowStage("lead", 1, "Lead data",
              "Who we're writing to: first name, position, company, size, location.",
              ("Position", "Company", "Employees", "Location"), None),
    FlowStage("research", 2, "Company research",
              "Reads the company's own site (email domain first, then a search if needed). "
              "Cached once per company and shared by every lead there.",
              ("1 Email domain (free)", "2 Tavily search, only if needed", "3 Summary + challenge tags"),
              "Only use what the research above literally says."),
    FlowStage("angle", 3, "Pick the angle",
              "Chooses 1-2 of the 27 challenges. Evidence from research first; otherwise 'common "
              "for your role', never a claim about them.",
              ("27 challenges", "Evidence-based", "Role-based fallback"),
              "Do NOT state or imply that THEY have the problem. Ask or invite, don't diagnose."),
    FlowStage("intro", 4, "Introduction & subject",
              "Greeting, a specific subject, and the opening line.",
              ("Subject", "Greeting", "Opening line"),
              "Subject: under 8 words, specific to them, no clickbait, no ALL CAPS"),
    FlowStage("offer", 5, "Offer: ready to help",
              "One sentence on how we help, drawn only from vrt_company.md "
              "(VEGA, EGOS, the 7 Stages assessment, the free strategy call).",
              ("VEGA", "EGOS", "Assessments"),
              "Do not quote prices, dates, day counts, or results beyond what the document states."),
    FlowStage("cta", 6, "Call to action",
              "Asks for the best phone number or a few times for a short call.",
              ("Free 45-min call", "Phone / times"),
              "Close by asking for the best phone number or a few times that work for a short call."),
    FlowStage("links", 7, "Sign-off & links",
              "Appended automatically: sign-off, vrtvega.com, the strategy-call link (click-tracked), "
              "the business address and the unsubscribe link.",
              ("vrtvega.com", "/talk-to-rajesh", "Address", "Unsubscribe"),
              None, editable=False),  # CAN-SPAM (§8): never staff-editable
)

EDITABLE_STAGE_IDS = tuple(s.id for s in FLOW_STAGES if s.editable)


def stage_title(stage_id: str) -> str:
    if stage_id == ALL_STAGES:
        return "Entire email"
    for s in FLOW_STAGES:
        if s.id == stage_id:
            return s.title
    return stage_id
