"""
Company research: what does this lead's company do, and does it look like it
is facing any of the 27 SMB challenges VRT helps with (app/knowledge)?

Cost-ordered on purpose -- the cheap, free path first:
  1. The lead's own email domain (free). A corporate address like
     jane@acmeindustrial.com gives us the website directly. Skipped for
     gmail/yahoo-style providers and schools, which say nothing about the
     company. The fetched page must actually mention the company's name, or
     it's treated as the wrong site and we fall through.
  2. Tavily search (1 credit per search; free tier is 1,000 a month) -- only
     when step 1 didn't produce a usable page, and only for companies about
     to be contacted.
  3. One GPT-4o-mini call to summarize and tag challenges.

One profile per company, cached by normalized company name, so every other
lead at the same company reuses it at no cost. "Not found" is cached too, so
a company that can't be researched isn't searched again on every draft; staff
can force a retry with Refresh.

Never invents: the summary and the challenge tags may only use what the pages
actually say. No evidence -> no tag.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.knowledge import loader as knowledge
from app.models.lead import Lead
from app.services import llm_service

logger = logging.getLogger(__name__)

# Providers and institutions whose domain tells us nothing about the company.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "zoho.com", "yandex.com", "rediffmail.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
}
_NON_COMPANY_SUFFIXES = (".edu", ".gov", ".mil", ".ac.in", ".edu.in", ".ac.uk", ".edu.au")

# Search results that are about the company, not the company's own site.
_SEARCH_RESULT_BLOCKLIST = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "crunchbase.com",
    "zoominfo.com", "bloomberg.com", "dnb.com", "glassdoor.com", "indeed.com", "yelp.com", "wikipedia.org",
    "apollo.io", "rocketreach.co", "mapquest.com", "bbb.org", "manta.com", "opencorporates.com",
    "maps.apple.com", "maps.google.com", "google.com", "tripadvisor.com", "foursquare.com", "yellowpages.com",
    "nextdoor.com", "pinterest.com", "reddit.com", "tiktok.com", "medium.com", "quora.com",
)

_GENERIC_NAME_WORDS = {
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llp", "plc",
    "group", "holdings", "the", "and", "of", "pvt", "private", "solutions", "services", "global",
}

MAX_PAGE_CHARS = 6000
FETCH_TIMEOUT = 8.0
_USER_AGENT = "Mozilla/5.0 (compatible; VRTLeadResearch/1.0; +https://www.vrtvega.com/)"


class ChallengeMatch(BaseModel):
    id: int
    name: str
    evidence: str  # what on the company's own pages points to this


class CompanyProfile(BaseModel):
    key: str
    company: str
    status: Literal["ok", "not_found"] = "ok"
    website: Optional[str] = None
    found_via: Optional[Literal["email_domain", "search"]] = None
    source_url: Optional[str] = None
    summary: Optional[str] = None
    stage_hint: Literal["starting", "growing", "established", "unknown"] = "unknown"
    challenges: list[ChallengeMatch] = Field(default_factory=list)
    researched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def company_key(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


# ---------------------------------------------------------------- fetching

class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text + " "
        elif not self._skip_depth:
            self.parts.append(text)


def html_to_text(html: str) -> tuple[str, str]:
    """Returns (title, visible text), collapsed and capped."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 -- malformed HTML: use whatever was parsed
        pass
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return parser.title.strip(), text[:MAX_PAGE_CHARS]


def fetch_page(url: str) -> Optional[tuple[str, str]]:
    """(title, text) for a URL, or None on any failure. Never raises."""
    try:
        resp = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html").lower():
            return None
        title, text = html_to_text(resp.text[:400_000])
        return (title, text) if text else None
    except Exception as e:  # noqa: BLE001
        logger.info("fetch_page failed for %s: %s", url, e)
        return None


def mentions_company(company: str, title: str, text: str) -> bool:
    """Guards the email-domain guess: the page has to actually be about this company."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if t not in _GENERIC_NAME_WORDS and len(t) >= 3]
    if not tokens:
        return False
    haystack = f"{title} {text}".lower()
    return any(t in haystack for t in tokens)


def corporate_domain(email: str) -> Optional[str]:
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in FREE_MAIL_DOMAINS or domain.endswith(_NON_COMPANY_SUFFIXES):
        return None
    return domain


def tavily_search(query: str) -> list[dict]:
    """One basic Tavily search (1 credit). [] when no key is configured or on any failure."""
    if not settings.tavily_api_key:
        return []
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={"query": query, "search_depth": "basic", "max_results": 5, "include_answer": False},
            timeout=15.0,
        )
        resp.raise_for_status()
        return list(resp.json().get("results", []))
    except Exception as e:  # noqa: BLE001
        logger.warning("Tavily search failed for %r: %s", query, e)
        return []


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _usable_result(result: dict) -> bool:
    host = _host(result.get("url", ""))
    return bool(host) and not any(host == b or host.endswith("." + b) for b in _SEARCH_RESULT_BLOCKLIST)


def _gather_site_text(base_url: str) -> Optional[tuple[str, str, str]]:
    """Homepage plus an About page if there is one -> (title, combined text, url used)."""
    home = fetch_page(base_url)
    if home is None:
        return None
    title, text = home
    root = base_url.rstrip("/")
    for path in ("/about", "/about-us"):
        about = fetch_page(root + path)
        if about and about[1][:200] != text[:200]:
            text = f"{text} {about[1]}"[: MAX_PAGE_CHARS * 2]
            break
    return title, text, base_url


# ------------------------------------------------------------ summarizing

def summarize(company: str, lead: Lead, source_text: str) -> dict:
    """One GPT-4o-mini call. Returns {summary, stage_hint, challenges:[{id, evidence}]}."""
    llm = llm_service._classification_llm()  # temperature 0: this is extraction, not writing
    prompt = f"""
You are researching a company so that {settings.company_name} (a business growth consulting
and leadership development firm) can write a relevant, honest note to someone who works there.

Company: {company}
Contact: {lead.full_name}, {lead.position or 'position unknown'}; company size on file: {lead.employee_count or 'unknown'}; location: {lead.location or 'unknown'}

Text taken from the company's own website:
\"\"\"{source_text}\"\"\"

The 27 business challenges we help with:
{knowledge.challenges_prompt_block()}

Return STRICT JSON (no markdown fences):
{{
  "summary": "2-3 plain sentences: what the company does and who it serves, using ONLY the text above",
  "stage_hint": "starting" | "growing" | "established" | "unknown",
  "same_company": true | false,
  "challenges": [{{"id": <number 1-27>, "evidence": "the specific thing in the text that points to it"}}]
}}

Rules:
- Only list a challenge if the text gives real evidence for it (e.g. an open-roles page suggests
  hiring pressure; language about rapid growth or new locations suggests chaotic periods or no
  systems). Quote or closely paraphrase that evidence. No evidence -> leave it out.
- It is normal and correct to return an empty challenges list. Never guess from the industry alone.
- At most 3 challenges.
- stage_hint: "starting" for a young company still setting up; "growing" for expansion, hiring or
  new offerings; "established" for a long-standing steady business; "unknown" if the text doesn't say.
- same_company: a name can match more than one business. Set false if this text describes a different
  kind of business than one where a {lead.position or 'person in that role'} at a company of
  {lead.employee_count or 'that size'} employees{' in ' + lead.location if lead.location else ''} would plausibly work
  (a different industry, a much smaller or larger operation, a different region), or you cannot tell. When in
  doubt, false -- a wrong company is worse than none.
- If the text does not look like it is about this company, set summary to "" and challenges to [].
"""
    resp = llm.invoke(prompt)
    return llm_service._extract_json(resp.content)


def _build_profile(company: str, lead: Lead, found: Optional[tuple[str, str, str]],
                   via: Optional[str]) -> CompanyProfile:
    key = company_key(company)
    if found is None:
        return CompanyProfile(key=key, company=company, status="not_found")
    _title, text, url = found
    try:
        data = summarize(company, lead, text)
    except Exception as e:  # noqa: BLE001 -- keep the lead usable without research
        logger.warning("Company summary failed for %s: %s", company, e)
        return CompanyProfile(key=key, company=company, status="not_found", website=url, found_via=via)  # type: ignore[arg-type]
    summary = (data.get("summary") or "").strip()
    if data.get("same_company") is False:
        logger.info("Discarding research for %s: %s doesn't look like the same company.", company, url)
        return CompanyProfile(key=key, company=company, status="not_found", website=url, found_via=via)  # type: ignore[arg-type]
    if not summary:
        return CompanyProfile(key=key, company=company, status="not_found", website=url, found_via=via)  # type: ignore[arg-type]
    matches = []
    for item in data.get("challenges") or []:
        if len(matches) == 3:
            break  # the cap counts valid tags, so a bad entry never uses up a slot
        raw_id = str(item.get("id", ""))
        ch = knowledge.challenge_by_id(int(raw_id)) if raw_id.isdigit() else None
        evidence = (item.get("evidence") or "").strip()
        if ch and evidence and ch.id not in {m.id for m in matches}:
            matches.append(ChallengeMatch(id=ch.id, name=ch.name, evidence=evidence))
    stage = data.get("stage_hint")
    return CompanyProfile(
        key=key, company=company, status="ok", website=url, found_via=via,  # type: ignore[arg-type]
        source_url=url, summary=summary,
        stage_hint=stage if stage in ("starting", "growing", "established") else "unknown",
        challenges=matches,
    )


def research_company(lead: Lead) -> CompanyProfile:
    """Does the actual research (network + LLM). Cheapest source first."""
    company = (lead.company or "").strip()

    domain = corporate_domain(lead.email)
    if domain:
        found = _gather_site_text(f"https://{domain}")
        if found and mentions_company(company, found[0], found[1]):
            return _build_profile(company, lead, found, "email_domain")

    query = f"{company} {lead.location or ''} official website".strip()
    for result in tavily_search(query):
        if not _usable_result(result):
            continue
        site = _gather_site_text(f"https://{_host(result['url'])}")
        if site and mentions_company(company, site[0], site[1]):
            return _build_profile(company, lead, site, "search")
        snippet = (result.get("content") or "").strip()
        if snippet and mentions_company(company, result.get("title", ""), snippet):
            return _build_profile(company, lead, (result.get("title", ""), snippet, result["url"]), "search")

    return _build_profile(company, lead, None, None)


# ------------------------------------------------------------ cache layer

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def get_cached(lead: Lead) -> Optional[CompanyProfile]:
    from app.services.storage_service import get_company_profile_store

    key = company_key(lead.company)
    return get_company_profile_store().get(key) if key else None


def get_or_research(lead: Lead, force: bool = False) -> Optional[CompanyProfile]:
    """
    The profile for this lead's company: cached if we've already looked
    (including a cached "not found"), researched and cached otherwise. A
    per-company lock means several leads from one company, drafted in
    parallel, trigger a single research run between them.
    None when the lead has no company, or in mock-integration mode (no
    network calls in local/synthetic testing).
    """
    from app.services.storage_service import get_company_profile_store

    key = company_key(lead.company)
    if not key or settings.use_mock_integrations:
        return None
    store = get_company_profile_store()
    with _lock_for(key):
        if not force:
            cached = store.get(key)
            if cached is not None:
                return cached
        profile = research_company(lead)
        store.save(profile)
        return profile
