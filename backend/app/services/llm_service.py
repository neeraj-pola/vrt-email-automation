"""
LLM service layer (CLAUDE.md §6).

Model split:
  - GPT-4o-mini: initial draft, follow-up drafts, reply classification,
    company-notification email, lead-confirmation email.
  - GPT-4o: chatbot-driven draft modification only (staff actively steering).

Uses LangChain chat models + structured output where the result needs to be
machine-consumed (classification, qualification), and plain HTML generation
for outreach copy.

Every function here returns/edits only the INNER content of an email (a
subject and a few paragraphs of body copy) -- never the branded shell
(header, site links, footer/unsubscribe link). That shell is applied
once, consistently, by app/services/email_template.py at send/preview time.
Keeping the LLM out of the shell means it can't drop the unsubscribe link,
mangle the footer, or have to faithfully reproduce a large HTML template on
every single edit.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.config import settings
from app.knowledge import loader as knowledge
from app.models.lead import Lead

COMPANY_PROCESS = (
    "Assessment → Hiring → Leadership Development → Strategic Alignment → "
    "Execution → Continuous Improvement"
)
PROJECT_CATEGORIES = [
    "the VEGA© cohort-based accelerator",
    "EGOS© (Entrepreneur Growth Operating System)",
    "the 7 Stages of Growth Assessment",
    "TriMetrix HD assessments",
    "Job Benchmarking",
    "Business Academy",
]

BRAND_VOICE = (
    "Professional yet approachable, action-oriented and solution-focused -- written like a "
    "real person on a small advisory team, not a marketing platform. Confident without being "
    "boastful; emphasize clarity and sustainable growth. No corporate filler, no hard-sell language."
)

# Appended verbatim to every customer-facing email rather than left to each
# generation to improvise a closing -- guarantees the same real person's
# name/title on every single touch instead of a sometimes-"Best,"-
# sometimes-"Warmly,"-sometimes-nothing sign-off, and means a lead who
# replies to any email in the sequence is replying to a consistent person.
_SIGN_OFF_HTML = f"<p>Best regards,<br>The Team<br>{settings.company_name}</p>"
_SIGN_OFF_INSTRUCTION = (
    'Do NOT write your own closing/sign-off line (no "Best," / "Warmly," / a name) -- '
    "one is appended automatically after your content."
)


def _drafting_llm(temperature: float = 0.7) -> ChatOpenAI:
    return ChatOpenAI(model=settings.drafting_model, api_key=settings.openai_api_key, temperature=temperature)


def _classification_llm() -> ChatOpenAI:
    return ChatOpenAI(model=settings.classification_model, api_key=settings.openai_api_key, temperature=0)


def _chatbot_llm() -> ChatOpenAI:
    return ChatOpenAI(model=settings.chatbot_modification_model, api_key=settings.openai_api_key, temperature=0.5)


class DraftOutput(BaseModel):
    subject: str
    body_html: str


def generate_initial_draft(lead: Lead) -> DraftOutput:
    """
    The FALLBACK first email: a fixed template with only the first name
    substituted. Used whenever personalization isn't available -- mock mode,
    no OpenAI key, or the personalized draft failed (see
    app/pipeline/draft_generation.py) -- so a lead always gets a draft. The
    normal path is generate_personalized_initial_draft.

    Every email after the first is AI-written and reads whatever was actually
    sent as context (generate_follow_up_draft), so follow-ups continue the
    conversation instead of repeating it.
    """
    first_name = lead.first_name or (lead.full_name.strip().split()[0] if lead.full_name.strip() else "there")
    subject = f"Thanks for Reaching Out, {first_name}!"
    body_html = (
        f"<p>Hi {first_name},</p>"
        "<p>Thanks for your inquiry! We'd love to help you build a business that grows beyond you.</p>"
        "<p>We'd love to set up a consultation to explain more about our approach and get "
        "some more information about your business. Could you reply to this email with the best "
        "phone number to reach you and a little about your goals, so we can get "
        "you set up with the next steps?</p>"
        "<p>Would you be available for a quick call, or would you like to schedule an appointment? "
        "We'd love to chat more about your goals!</p>"
        f"{_SIGN_OFF_HTML}"
    )
    return DraftOutput(subject=subject, body_html=body_html)


class PersonalizedDraft(BaseModel):
    subject: str
    body_html: str
    # Which of the 27 challenges the email was framed around, and why -- shown
    # to staff next to the draft so they can see (and overrule) the angle.
    note: str = ""


def generate_personalized_initial_draft(lead: Lead, profile=None, changes=None, temperature: float = 0.7) -> PersonalizedDraft:
    """
    The first email, written for this person: their role, their company, and
    -- when the company research found real evidence -- a challenge from
    VRT's list of 27 that company looks like it is facing.

    `profile` is a company_research.CompanyProfile (or None when there is
    nothing researched, in which case the angle comes from the person's role
    and company size alone, framed as something common at that stage -- never
    as a claim about them).

    `changes` are the staff instructions from the Modify draft page that
    cover this lead (app/services/draft_changes_service.py); they go into the
    prompt as a block that explicitly cannot override the hard rules.

    Honesty rules are in the prompt rather than left to chance: no invented
    facts about the company, no stating that they HAVE a problem unless a page
    said so, no stats or prices, nothing promised beyond a call.
    """
    from app.services import draft_changes_service

    llm = _drafting_llm(temperature)
    first_name = lead.first_name or (lead.full_name.strip().split()[0] if lead.full_name.strip() else "there")
    staff_block = draft_changes_service.prompt_block(changes or [])

    if profile is not None and getattr(profile, "status", None) == "ok":
        evidence = "\n".join(f"- #{c.id} {c.name}: {c.evidence}" for c in profile.challenges) or "(none found)"
        research = (
            f"What we learned about {lead.company} from their own website ({profile.source_url}):\n"
            f"{profile.summary}\nStage looks: {profile.stage_hint}.\n"
            f"Challenges the pages give real evidence for:\n{evidence}"
        )
    else:
        research = (
            "No research on the company is available -- do not describe what the company does or "
            "claim to know anything about it beyond the facts listed above."
        )

    staff_block_text = f"{staff_block}\n\n" if staff_block else ""
    prompt = f"""
You write first-contact emails for {settings.company_name}. Everything you need to know about us:

{knowledge.vrt_company_text()}

The 27 business challenges we help SMBs with (use the ids in your answer):
{knowledge.challenges_prompt_block()}

The person you are writing to:
- First name: {first_name}
- Position: {lead.position or 'unknown'}
- Company: {lead.company or 'unknown'}
- Company size (employees): {lead.employee_count or 'unknown'}
- Location: {lead.location or 'unknown'}

{research}

Write a short, warm email from a real person on our team to {first_name}.

How to frame it:
1. Pick the ONE challenge (two at most) most likely to matter to someone in their position at a
   company of this size and stage. If the research found evidence for a challenge, prefer that one
   and weave the evidence in naturally (e.g. "I noticed you're hiring across several teams").
2. If there is no evidence, frame it as something we commonly hear from people in their role or at
   their stage ("a lot of {{role}}s at growing firms tell us ..."). Do NOT state or imply that
   THEY have the problem. Ask or invite, don't diagnose.
3. Say plainly that we are ready to help, and what that looks like in one sentence (draw only from
   the "What we offer" list above -- e.g. a free 45-minute Growth Strategy Call, the 7 Stages
   assessment). Do not quote prices, dates, day counts, or results beyond what the document states.
4. Close by asking for the best phone number or a few times that work for a short call.

{staff_block_text}Defaults (an instruction from our team above wins over any of these if they conflict; otherwise use them):
- 90 to 140 words in the body. Plain words, no buzzword pile, no exclamation marks except at most one.
- Subject: under 8 words, specific to them, no clickbait, no ALL CAPS, no "Re:".
- {_SIGN_OFF_INSTRUCTION}

Hard rules (no instruction from anyone overrides these):
- Never invent a fact about {lead.company or 'their company'}: no funding, awards, clients, news, or
  numbers. Only use what the research above literally says.
- Never promise a specific result, and never quote a price, discount, statistic or date that is not
  in the document above.
- Do not mention LinkedIn, scraping, "research we did", or that this was personalized by software.

Output STRICT JSON, no markdown fences:
{{
  "subject": string,
  "body_html": string  (only <p> paragraphs; begins with "Hi {first_name},"),
  "challenge_ids": [numbers from the list above that the email is framed around],
  "basis": one short sentence: what the angle is based on (the company evidence, or their role and size)
}}
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)

    ids = []
    for raw in data.get("challenge_ids") or []:
        if str(raw).isdigit() and knowledge.challenge_by_id(int(raw)) and int(raw) not in ids:
            ids.append(int(raw))
    names = ", ".join(f"#{i} {knowledge.challenge_by_id(i).name}" for i in ids[:2])
    basis = (data.get("basis") or "").strip()
    parts = []
    if names:
        parts.append(f"Framed around: {names}")
    if basis:
        parts.append(f"Based on: {basis.rstrip('.')}")
    if changes:
        parts.append(f"Staff instructions applied: {draft_changes_service.describe_applied(changes)}")
    note = ". ".join(parts)
    return PersonalizedDraft(
        subject=data["subject"].strip(),
        body_html=f'{data["body_html"].strip()}\n{_SIGN_OFF_HTML}',
        note=note,
    )


# Distinct instructions per touch, so follow-up 1/2/3 don't all read like the
# same email with a different number on it -- each has a different job to do.
_FOLLOW_UP_ANGLE = {
    1: "A gentle, brief nudge. Assume they're just busy, not uninterested. Keep it short -- "
       "shorter than the first email, not a repeat of it.",
    2: "Try a different angle than the first two emails so far -- e.g. lead with a different "
       "program or a specific detail (timeline flexibility, a relevant assessment or program) "
       "rather than repeating the same ask a third time.",
    3: "This is the last automatic follow-up before we stop reaching out. Write the main body "
       "warmly and low-pressure -- do NOT write your own closing line about this being the last "
       "email; a standard closing note about that is appended automatically after your content, "
       "so just focus on a genuine, warm final check-in without pre-empting that line.",
}

# Appended verbatim after the LLM's content on the final follow-up, rather
# than trusted to the model's own wording every time -- a generation can
# vary in whether it actually spells out "we'll stop emailing after this,"
# and that's the one part of this email that has to be there without fail.
_FINAL_FOLLOW_UP_NOTICE = (
    "<p><em>This is our last check-in for now — we won't keep emailing after this, "
    "but reach out anytime you're ready and we'll be glad to pick things back up.</em></p>"
)


def generate_follow_up_draft(lead: Lead, previous_subject: str, previous_body_html: str) -> DraftOutput:
    """
    Stage 8 (§6/§8): follow-up when a lead has gone quiet.

    previous_subject/previous_body_html is whatever was actually last sent
    (the initial email, or the prior follow-up) -- without it the model has
    no idea what it already said and just writes a near-duplicate of the
    first pitch with "just checking in" tacked on.
    """
    llm = _drafting_llm()
    touch_number = lead.follow_up_count + 1
    angle = _FOLLOW_UP_ANGLE.get(touch_number, _FOLLOW_UP_ANGLE[3])
    prompt = f"""
You are writing FOLLOW-UP #{touch_number} (of max {settings.max_follow_ups}) on behalf of
{settings.company_name}. Brand voice: {BRAND_VOICE}
Process language, if useful: {COMPANY_PROCESS}

The lead ({lead.full_name}) has not replied. Here is EXACTLY what was already sent to them
-- read it, because this follow-up must continue that conversation, not restate it:

Previous subject: {previous_subject}
Previous email body:
{previous_body_html}

This follow-up's specific job (this is touch #{touch_number}): {angle}

Do not repeat the previous email's phrasing or structure. Keep it noticeably shorter than
the previous message. It should read like a real person circling back, referencing that
this is a continuation (e.g. "wanted to follow up on my last note") without quoting it.

{_SIGN_OFF_INSTRUCTION}

Output STRICT JSON with keys "subject" and "body_html". body_html is ONLY the message
body (a few short paragraphs in <p> tags) -- no logo/footer/unsubscribe line, those
are added automatically. No markdown fences.
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    body_html = data["body_html"]
    if touch_number >= settings.max_follow_ups:
        body_html = f"{body_html}\n{_FINAL_FOLLOW_UP_NOTICE}"
    body_html = f"{body_html}\n{_SIGN_OFF_HTML}"
    return DraftOutput(subject=data["subject"], body_html=body_html)


def generate_unclear_reply_draft(lead: Lead, previous_subject: str, previous_body_html: str) -> DraftOutput:
    """§6.7: 'unclear' classification re-enters the normal drafting flow, same style."""
    llm = _drafting_llm()
    prompt = f"""
You are replying to {lead.full_name}, whose last reply to our outreach was vague or
ambiguous (could be an auto-response, or just unclear interest). Here is EXACTLY what we
sent them right before that reply, for context -- don't repeat it, just make sure this
reply continues naturally from it:

Previous subject: {previous_subject}
Previous email body:
{previous_body_html}

Write a brief, warm, low-pressure email in the same style as our normal outreach for
{settings.company_name} ({BRAND_VOICE}) that gently re-asks about their interest,
location, timeline, and availability for a call, without assuming they already said no
or yes.

{_SIGN_OFF_INSTRUCTION}

Output STRICT JSON with keys "subject" and "body_html". body_html is ONLY the message
body (a few short paragraphs in <p> tags) -- no logo/footer/unsubscribe line, those
are added automatically. No markdown fences.
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return DraftOutput(subject=data["subject"], body_html=f'{data["body_html"]}\n{_SIGN_OFF_HTML}')


class ClassificationResult(BaseModel):
    classification: Literal["interested", "not_interested", "unclear"]
    requested_availability: Optional[str] = None


def _last_sent_content(lead: Lead) -> tuple[str, str]:
    """
    Most recent version actually emailed to this lead, for prompt context.
    Mirrors outreach_graph._last_sent_content exactly (duplicated rather
    than imported -- outreach_graph already imports this module, so the
    reverse import would be circular); falls back to the current draft.
    """
    for version in reversed(lead.draft_history):
        if version.sent:
            return version.subject, version.body_html
    return lead.current_draft_subject or "", lead.current_draft_body_html or ""


def classify_reply(lead: Lead, reply_text: str) -> ClassificationResult:
    """
    Stage 7 (§6.7): classify a reply. qualified_status is no longer proposed
    from reply content/signals here -- staff decided qualification should
    track engagement, not be gated on things like company size or distance
    from the service area. The only automatic qualified_status writes now
    live in outreach_graph.py: not_qualified on a not_interested reply, on
    exhaustion (no reply after all follow-ups), or on unsubscribe -- nothing
    else. "qualified" is a staff call, made from the dashboard once they've
    actually talked to the lead.
    """
    llm = _classification_llm()
    prev_subject, prev_body = _last_sent_content(lead)
    # A short reply like "Sure" or "Yes" is unreadable in isolation -- it
    # only makes sense against what was actually asked (e.g. "let me know
    # if you're available for a quick chat"). Caught live 2026-10-07: a
    # one-word "Sure" classified "unclear" with no context of the question
    # it was answering, sitting unreviewed instead of surfacing as a lead
    # agreeing to be contacted. Context is best-effort (a lead with no sent
    # history yet still classifies fine on the reply text alone).
    context_block = (
        f'What we sent them (most recent email):\nSubject: "{prev_subject}"\n{prev_body}\n\n'
        if prev_subject or prev_body else ""
    )
    prompt = f"""
Classify this reply from a lead of {settings.company_name} (a business growth
consulting and leadership development firm in {settings.company_address}).

{context_block}Reply text:
\"\"\"{reply_text}\"\"\"

Classify as exactly one of: "interested", "not_interested", "unclear".
"unclear" covers vague replies and auto-responses (e.g. out-of-office).

A short affirmative reply ("Sure", "Yes", "Sounds good", "OK") only makes
sense read against what we sent them above -- if it's answering "yes" to
being contacted, available for a call, or moving forward, classify it
"interested", not "unclear". Reserve "unclear" for replies that are
genuinely ambiguous even in that context (e.g. a one-word reply to a
multi-part question where it's not clear which part it answers).

Whether to proceed despite a logistical obstacle (out of service area, budget,
timing, etc.) is a staff/business decision, not yours to make by closing out
the lead. If the reply expresses genuine positive sentiment about the company
or its work -- even alongside a stated practical obstacle like being outside
the service area -- classify it as "interested" so a human reviews it, not
"not_interested". Reserve "not_interested" for replies that actually decline
or reject the service itself (e.g. "not interested", "please stop contacting
us", "we went with someone else", explicit disinterest with no positive
sentiment toward the company).

A reply that is essentially just a contact card -- a name, address, phone
number, and/or email, with no other message text (e.g. an email signature
block sent as the entire reply) -- is also "interested", not "unclear".
Volunteering that contact info in reply to the outreach is itself a signal
someone wants to be reached, even with nothing else said. Reserve "unclear"
for replies that are genuinely vague or non-committal, or are auto-responses
(e.g. out-of-office), not for a bare contact card.

Also extract, only if actually stated in the reply (else null):
- Any availability/timing they gave for a call.

Output STRICT JSON only, matching this shape (no markdown fences):
{{
  "classification": "interested" | "not_interested" | "unclear",
  "requested_availability": string or null
}}
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return ClassificationResult(**data)


class ImportNotesQualification(BaseModel):
    qualified_status: Optional[Literal["qualified", "not_qualified"]] = None
    reasoning: Optional[str] = None


def propose_qualification_from_import_notes(lead: Lead, notes: str) -> ImportNotesQualification:
    """
    A lead sheet can arrive with real prior work already in it -- a "Notes"
    column from before this system existed, e.g. "called 8/12, too small, not
    interested" or "already qualified, wants the VEGA cohort, budget approved".
    That context shouldn't just vanish on import (§4's AI-proposed/staff-
    confirmable qualified_status exists for exactly this kind of signal).

    Deliberately separate from classify_reply: this isn't a reply to our
    outreach (ai_response_classification stays untouched -- no reply has
    happened in this system yet), it's a proposal from pre-existing staff
    notes, using the same §5 signals. Returns null/null when the notes say
    nothing that actually bears on qualification, rather than guessing --
    staff still have the final say either way (qualified_status_source
    stays "ai", overridable same as any AI proposal).
    """
    llm = _classification_llm()
    prompt = f"""
A staff member's pre-existing note about a lead of {settings.company_name} (a business growth consulting
firm in {settings.company_address}) is being imported alongside them.

Lead context:
- Company: {lead.company or 'unknown'}, position: {lead.position or 'unknown'}
- Company size (employees): {lead.employee_count or 'unknown'}
- Location: {lead.location or 'unknown'}

Staff note:
\"\"\"{notes}\"\"\"

If (and only if) this note contains enough signal to judge qualification, evaluate these
signals and propose qualified_status as "qualified" or "not_qualified":
  - Seniority/decision power (owner, founder, CEO, or senior leader is a strong positive signal)
  - Company size (roughly 20+ employees, $2M-$10M revenue is our core fit; a very small or very large company is a weaker fit)
  - Location relative to our service area (US-based preferred) if mentioned
  - Stated budget or cost sensitivity, if volunteered
  - Desired project start timeline
  - Project type/category and how specific the request is
Provide brief reasoning staff can review.

If the note doesn't actually speak to qualification (e.g. just a callback time, or too
vague to judge), return null for both fields -- don't guess.

Output STRICT JSON only, matching this shape (no markdown fences):
{{
  "qualified_status": "qualified" | "not_qualified" | null,
  "reasoning": string or null
}}
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return ImportNotesQualification(**data)


def generate_company_notification_email(lead: Lead, classification: ClassificationResult, reply_text: str) -> DraftOutput:
    """
    §6.7: notify the company of an interested lead.

    No AI-proposed qualification here (staff instruction: qualification is a
    staff call now, not an AI signal-weighing proposal) -- this just
    summarizes the lead and reply and prompts staff to review and mark
    qualified/not qualified themselves.
    """
    llm = _drafting_llm()
    prompt = f"""
Write an internal notification email to {settings.company_name} staff summarizing a
new interested lead, for {settings.company_notification_email or 'the sales inbox'}.

Lead: {lead.full_name}, {lead.email}
Company: {lead.company or 'unknown'} | Position: {lead.position or 'unknown'} | Employees: {lead.employee_count or 'unknown'} | Location: {lead.location or 'unknown'}

Lead's reply:
\"\"\"{reply_text}\"\"\"

Stated availability: {classification.requested_availability or 'not mentioned'}

Write it as a clear, scannable internal email (not customer-facing tone): lead
summary, the reply content, stated availability, and a prompt for staff to review
the reply, decide qualified/not qualified, and confirm a call time on the dashboard.

Output STRICT JSON with keys "subject" and "body_html" (valid HTML, no unsubscribe
footer needed -- internal email, no markdown fences).
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return DraftOutput(subject=data["subject"], body_html=data["body_html"])


def generate_confirmation_email(lead: Lead, confirmed_time: str) -> DraftOutput:
    """§6.7: once staff confirm a call time, send the lead a confirmation."""
    llm = _drafting_llm()
    prompt = f"""
Write a short, warm confirmation email to {lead.full_name} from
{settings.company_name} confirming a call/appointment at: {confirmed_time}.
Brand voice: {BRAND_VOICE}. Keep it brief -- just confirm the time and thank them.
Someone from our team will be the one reaching out -- you can mention that
naturally in the body if it fits.

{_SIGN_OFF_INSTRUCTION}

Output STRICT JSON with keys "subject" and "body_html". body_html is ONLY the message
body (a few short paragraphs in <p> tags) -- no logo/footer/unsubscribe line, those
are added automatically. No markdown fences.
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return DraftOutput(subject=data["subject"], body_html=f'{data["body_html"]}\n{_SIGN_OFF_HTML}')


def apply_chatbot_modification(current_draft: DraftOutput, instruction: str) -> DraftOutput:
    """§6/§9: staff-directed rewrite/structural change to a draft, via GPT-4o."""
    llm = _chatbot_llm()
    prompt = f"""
Current email draft -- subject and body content ONLY (the logo, site links,
and footer/unsubscribe line are added automatically at send time and are not shown
here; do not add them yourself):
Subject: {current_draft.subject}
Body HTML:
{current_draft.body_html}

A staff member at {settings.company_name} has requested this change:
\"\"\"{instruction}\"\"\"

Apply the requested change and return the full revised draft (subject + body content
only, a few short paragraphs in <p> tags). Keep brand voice: {BRAND_VOICE}.

Output STRICT JSON with keys "subject" and "body_html" (no markdown fences).
"""
    resp = llm.invoke(prompt)
    data = _extract_json(resp.content)
    return DraftOutput(subject=data["subject"], body_html=data["body_html"])


def answer_lead_question(lead: Lead, question: str, history: list[dict[str, str]]) -> str:
    """
    Staff-facing advisor, available for a lead at ANY point -- including
    after the initial email is already sent and the draft is locked. Unlike
    apply_chatbot_modification, this never touches current_draft_*; it's for
    "what should we say if they ask about X" / talking-points / dialogue
    help, using GPT-4o with full context on the lead so far. history is the
    prior turns of this conversation ({"role": "staff"|"assistant", "text"}),
    most recent last, so follow-up questions have context.
    """
    llm = _chatbot_llm()
    history_text = "\n".join(f"{turn['role']}: {turn['text']}" for turn in history[-8:])
    prompt = f"""
You are advising a staff member at {settings.company_name} on how to handle a specific
lead -- you are NOT writing an email to the lead, you're answering the staff member's
question so they know what to say. Brand voice to keep in mind for any suggested
wording: {BRAND_VOICE}
Company process: {COMPANY_PROCESS}
Qualification signals worth knowing about (§5): the lead's seniority/decision power,
company size (roughly 20+ employees is our core fit), location relative to our
service area, stated budget/cost sensitivity, desired start timeline, and how
specific their goals are.

Lead so far:
- Name: {lead.full_name}
- Company: {lead.company or 'n/a'}, position: {lead.position or 'n/a'}
- Company size (employees): {lead.employee_count or 'n/a'}, location: {lead.location or 'n/a'}
- Their latest reply: {lead.last_reply_text or 'no reply yet'}
- Email status: {lead.email_status}, follow-ups sent: {lead.follow_up_count}
- AI reply classification: {lead.ai_response_classification or 'no reply yet'}
- Requested availability: {lead.requested_availability or 'not stated'}
- Qualification: {lead.qualified_status} (source: {lead.qualified_status_source or 'n/a'})
- Qualification reasoning: {lead.qualified_status_reasoning or 'n/a'}
- Staff notes: {lead.notes or 'none'}

Prior conversation in this advisor chat (if any):
{history_text or '(none yet)'}

Staff member's question:
\"\"\"{question}\"\"\"

Answer directly and concisely -- a couple of short paragraphs or a short list, plain
text (no HTML, no markdown headers). If useful, you may suggest exact wording the
staff member could say or send, but be clear that's a suggestion, not something
already sent.
"""
    resp = llm.invoke(prompt)
    return resp.content.strip()


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
