"""
Gmail API service (CLAUDE.md §7): send + read via the same OAuth connection,
so replies can be detected and threaded, which raw SMTP cannot do.

Testing: a developer's own Gmail. Production: same code, re-auth against the
company mailbox (§7) -- no architecture change needed.

Set USE_MOCK_INTEGRATIONS=true (.env) to use an in-memory mock instead of
live Gmail, for the synthetic-lead test pass (§13.1) before OAuth is wired up.
"""
from __future__ import annotations

import base64
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape
from typing import Callable, Optional

from app.config import settings
from app.services.storage_service import PipelineStateStore

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Gmail API quota is shared across every call this OAuth identity makes --
# send() and poll_new_replies() are not separately metered. With hundreds of
# leads already sent, poll_new_replies makes one threads().get call per
# active thread, every tick; at real scale that burst alone can trip
# rateLimitExceeded, and because the bucket is shared, a legitimate send()
# landing in or just after that burst gets caught in the same 403 -- this is
# what silently stalled real sends once the active-lead count grew past
# ~250-300 (caught 2026-09-10: 0 sends recorded on a day staff had actually
# clicked send, immediate rateLimitExceeded on startup).
_RETRYABLE_STATUSES = {403, 429, 500, 503}


def _execute_with_backoff(request, max_retries: int = 6):
    """Exponential backoff + jitter, per Google's own recommended retry pattern.
    Retries on 403/429/500/503 broadly rather than parsing the error reason
    string -- googleapiclient's reason parsing isn't a stable public API
    across versions, and a genuine non-transient 403 (e.g. a real permission
    problem) still surfaces correctly, just after these retries are
    exhausted instead of on the first attempt."""
    from googleapiclient.errors import HttpError

    delay = 1.0
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
            if status not in _RETRYABLE_STATUSES or attempt == max_retries - 1:
                raise
            time.sleep(delay + random.uniform(0, delay * 0.5))
            delay *= 2


class HistoryExpiredError(Exception):
    """Gmail only retains mailbox history for a limited window -- raised
    when a stored history_id is too old for history.list to use. The
    caller's response is always the same: fall back to poll_new_replies and
    re-bootstrap a fresh cursor, not treat this as fatal."""


@dataclass
class SentMessage:
    message_id: str
    thread_id: str


@dataclass
class InboundReply:
    thread_id: str
    from_email: str
    body_text: str
    message_id: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class GmailClientBase:
    def send(self, to: str, subject: str, html_body: str, thread_id: Optional[str] = None,
              in_reply_to: Optional[str] = None) -> SentMessage:
        raise NotImplementedError

    def poll_new_replies(self, thread_ids: set[str]) -> list[InboundReply]:
        """
        thread_ids: the specific threads worth checking (the caller's active
        leads) -- NOT "poll the whole inbox." Fetching every message in the
        mailbox and asking the database about each one doesn't scale (a
        personal or company inbox has plenty of mail with nothing to do with
        any lead) and was the actual cause of a real bug: one database query
        per inbox message, every tick, forever.

        This is the slow-but-proven path: one API call per thread. Kept
        as-is and used as the automatic fallback for the history.list-based
        polling below (see poll_new_replies_with_history_cursor) -- first
        run ever, or whenever the history cursor turns out to be stale/
        invalid, or genuinely any unexpected failure in the fast path.
        """
        raise NotImplementedError

    def get_history_id(self) -> str:
        """Current position in this mailbox's change history -- the baseline
        a later list_new_message_ids_since call measures forward from."""
        raise NotImplementedError

    def list_new_message_ids_since(self, history_id: str) -> tuple[list[tuple[str, str]], str]:
        """
        Every message added to the *entire* mailbox since history_id, as
        (message_id, thread_id) pairs -- one API call (paginated only if a
        lot happened), regardless of how many threads are being watched.
        Returns (new_message_ids, next_history_id). Raises if history_id is
        no longer valid (Gmail only retains history for a limited window) --
        the caller falls back to poll_new_replies when that happens.
        """
        raise NotImplementedError

    def get_message_by_id(self, message_id: str) -> Optional[InboundReply]:
        """Full content of one message, or None if it's our own sent mail
        (see _message_to_reply) rather than something from a lead."""
        raise NotImplementedError


class MockGmailClient(GmailClientBase):
    """In-memory stand-in used when USE_MOCK_INTEGRATIONS=true."""

    def __init__(self):
        self._sent: list[dict] = []
        self._inbox: list[InboundReply] = []
        # Append-only, separate from _inbox on purpose: poll_new_replies
        # dequeues _inbox (matches real per-thread polling semantics tests
        # already rely on), but Gmail's real history.list is a cursor into
        # an ever-growing log, never a queue you drain. _history simulates
        # that: nothing ever gets removed from it, only the caller's cursor
        # (an index into this list) advances.
        self._history: list[InboundReply] = []
        self._counter = 0
        self._history_expired = False  # test helper, see raise_history_expired_once

    def send(self, to: str, subject: str, html_body: str, thread_id: Optional[str] = None,
              in_reply_to: Optional[str] = None) -> SentMessage:
        self._counter += 1
        msg_id = f"mock-msg-{self._counter}"
        tid = thread_id or f"mock-thread-{self._counter}"
        self._sent.append({
            "to": to, "subject": subject, "html_body": html_body,
            "thread_id": tid, "message_id": msg_id,
        })
        return SentMessage(message_id=msg_id, thread_id=tid)

    def poll_new_replies(self, thread_ids: set[str]) -> list[InboundReply]:
        matched = [msg for msg in self._inbox if msg.thread_id in thread_ids]
        self._inbox = [msg for msg in self._inbox if msg.thread_id not in thread_ids]
        return matched

    def get_history_id(self) -> str:
        return str(len(self._history))

    def list_new_message_ids_since(self, history_id: str) -> tuple[list[tuple[str, str]], str]:
        if self._history_expired:
            self._history_expired = False
            raise HistoryExpiredError("mock: history_id no longer valid")
        new = self._history[int(history_id):]
        return [(r.message_id, r.thread_id) for r in new], str(len(self._history))

    def get_message_by_id(self, message_id: str) -> Optional[InboundReply]:
        reply = next((r for r in self._history if r.message_id == message_id), None)
        # Mirror LiveGmailClient's _message_to_reply filtering, so the mock
        # stays a faithful stand-in -- a test injecting a reply "from" our
        # own sender address should behave exactly like the real client
        # would (see _is_our_own_address).
        if reply is not None and _is_our_own_address(reply.from_email):
            return None
        return reply

    # test helper: simulate a lead replying
    def inject_reply(self, thread_id: str, from_email: str, body_text: str,
                      received_at: Optional[datetime] = None):
        self._counter += 1
        reply = InboundReply(
            thread_id=thread_id, from_email=from_email, body_text=body_text,
            message_id=f"mock-reply-{self._counter}",
            received_at=received_at or datetime.now(timezone.utc),
        )
        self._inbox.append(reply)
        self._history.append(reply)

    # test helper: force the next list_new_message_ids_since call to behave
    # like Gmail rejecting a stale history_id, to exercise the fallback path.
    def raise_history_expired_once(self):
        self._history_expired = True

    @property
    def sent_log(self) -> list[dict]:
        return self._sent


class LiveGmailClient(GmailClientBase):
    """Real Gmail API client. Requires google-api-python-client + OAuth setup."""

    def __init__(self):
        import json

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        # Two ways to supply the OAuth token, checked in this order:
        #
        # 1. GMAIL_OAUTH_TOKEN_JSON env var -- the *contents* of a
        #    gmail_token.json, pasted whole. This is what production (§11)
        #    uses: Railway's filesystem is rebuilt fresh on every deploy, so
        #    a file under credentials/ (gitignored on purpose -- it's a
        #    secret) simply wouldn't exist there, and there's no browser to
        #    complete an interactive OAuth flow on a headless server anyway.
        #    Run the flow once locally, then paste the resulting token
        #    file's contents into Railway's env var settings.
        #
        # 2. credentials/gmail_token.json on disk -- local dev only, where
        #    running the one-time interactive flow and having it write a
        #    file next to the code is the easy path.
        creds = None
        token_json = settings.gmail_oauth_token_json
        token_file = settings.gmail_oauth_token_file
        if token_json:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        elif os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif token_json:
                # A token supplied via env var with no valid refresh_token
                # can't be repaired here -- there's no browser to reauthorize
                # from inside a deployed container. Fail loudly rather than
                # silently falling through to a local interactive flow that
                # would hang forever on a server.
                raise RuntimeError(
                    "GMAIL_OAUTH_TOKEN_JSON is set but the token is invalid and has no usable "
                    "refresh_token. Re-run the local OAuth flow and paste the fresh token JSON."
                )
            else:
                client_secret_json = settings.gmail_oauth_client_secret_json
                flow = (
                    InstalledAppFlow.from_client_config(json.loads(client_secret_json), SCOPES)
                    if client_secret_json
                    else InstalledAppFlow.from_client_secrets_file(
                        settings.gmail_oauth_client_secret_file, SCOPES
                    )
                )
                creds = flow.run_local_server(port=0)
            if not token_json:
                os.makedirs(os.path.dirname(token_file), exist_ok=True)
                with open(token_file, "w") as f:
                    f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

    def send(self, to: str, subject: str, html_body: str, thread_id: Optional[str] = None,
              in_reply_to: Optional[str] = None) -> SentMessage:
        # HTML-only mail (no plain-text alternative) is itself a spam signal
        # -- every legitimate bulk sender includes both parts. multipart/
        # alternative with text/plain first, text/html last (the part a
        # client prefers is the *last* one it understands).
        message = MIMEMultipart("alternative")
        message.attach(MIMEText(_html_to_plain_text(html_body), "plain"))
        message.attach(MIMEText(html_body, "html"))
        message["to"] = to
        message["subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to

        # RFC 8058 one-click unsubscribe header. This -- not just a footer
        # link -- is what Gmail/Yahoo/etc actually weigh for inbox placement,
        # and is what puts the native "Unsubscribe" pill next to our name in
        # the recipient's Gmail UI. Pulled from the footer link already in
        # html_body so callers don't need to pass it separately.
        unsub_match = re.search(r'href="([^"]*public/unsubscribe[^"]*)"', html_body)
        if unsub_match:
            message["List-Unsubscribe"] = f"<{unsub_match.group(1)}>"
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        sent = _execute_with_backoff(self._service.users().messages().send(userId="me", body=body))
        return SentMessage(message_id=sent["id"], thread_id=sent["threadId"])

    def poll_new_replies(self, thread_ids: set[str]) -> list[InboundReply]:
        # Fetch only the specific threads we're actually waiting on (each
        # active lead's own thread_id) -- NOT a scan of the whole inbox.
        # Previously this listed up to 100 recent inbox messages regardless
        # of relevance (a personal Gmail account's job alerts, shopping
        # receipts, etc. included) and the caller checked every single one
        # against the database -- one query per unrelated email, every tick,
        # forever. Cost scales with active-lead count now, not inbox size.
        #
        # Deliberately NOT filtered on is:unread within a thread -- staff/
        # the developer's own account routinely reads messages while
        # replying, which would mark them read before this poll ever runs.
        # The caller (tick.py) dedups by comparing each reply's received_at
        # against the lead's last_sent_at instead, so a stale/already-
        # handled reply is never reprocessed.
        replies: list[InboundReply] = []
        for i, thread_id in enumerate(thread_ids):
            # A small proactive gap between calls, on top of the reactive
            # backoff inside _execute_with_backoff -- at hundreds of active
            # threads, firing every call back-to-back with zero spacing is
            # what bursts past the per-user quota in the first place, not
            # just what happens to recover from it after the fact.
            if i > 0:
                time.sleep(0.15)
            try:
                thread = _execute_with_backoff(
                    self._service.users().threads().get(userId="me", id=thread_id, format="full")
                )
            except Exception:  # noqa: BLE001 -- a deleted/inaccessible thread must not kill the whole tick
                continue
            for msg in thread.get("messages", []):
                reply = _message_to_reply(msg)
                if reply is not None:
                    replies.append(reply)
        return replies

    def get_history_id(self) -> str:
        profile = _execute_with_backoff(self._service.users().getProfile(userId="me"))
        return str(profile["historyId"])

    def list_new_message_ids_since(self, history_id: str) -> tuple[list[tuple[str, str]], str]:
        from googleapiclient.errors import HttpError

        found: list[tuple[str, str]] = []
        latest_history_id = history_id
        page_token = None
        while True:
            try:
                resp = _execute_with_backoff(
                    self._service.users().history().list(
                        userId="me", startHistoryId=history_id, historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                )
            except HttpError as e:
                status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
                if status == 404:
                    raise HistoryExpiredError(f"history_id {history_id} is no longer valid") from e
                raise
            latest_history_id = resp.get("historyId", latest_history_id)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    found.append((msg["id"], msg["threadId"]))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return found, str(latest_history_id)

    def get_message_by_id(self, message_id: str) -> Optional[InboundReply]:
        try:
            msg = _execute_with_backoff(
                self._service.users().messages().get(userId="me", id=message_id, format="full")
            )
        except Exception:  # noqa: BLE001 -- a message that vanished/is inaccessible must not kill the tick
            return None
        return _message_to_reply(msg)


# Matches the line where quoted history starts in a plain-text reply, so it
# can be cut off before classification/keyword checks. Otherwise our own
# footer text ("...Unsubscribe") comes back quoted inside the lead's reply
# and gets misread as the lead asking to unsubscribe.
_QUOTE_START_PATTERNS = [
    re.compile(r"^\s*On .+ wrote:\s*$", re.MULTILINE),          # Gmail/Apple Mail
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:\s.*$", re.MULTILINE),                 # Outlook-style quoted header block
    re.compile(r"^>.*$", re.MULTILINE),                          # first quoted (">") line
]


def _strip_quoted_reply(text: str) -> str:
    earliest_cut = len(text)
    for pattern in _QUOTE_START_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_cut:
            earliest_cut = match.start()
    return text[:earliest_cut].strip()


def _html_to_plain_text(html: str) -> str:
    """Best-effort text/plain fallback for the multipart alternative -- not
    meant to be pretty, just readable, since real clients will show the HTML
    part and this is mainly there to avoid an HTML-only spam signal."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    text = re.sub(r"(?i)</(p|div|tr|table)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _is_our_own_address(from_email: str) -> bool:
    """True for a message sent by this mailbox itself, not received from a
    lead -- see poll_new_replies for why this matters."""
    return from_email.strip().lower() == settings.gmail_sender_address.strip().lower()


def _find_part(payload: dict, mime_type: str) -> Optional[str]:
    if payload.get("mimeType") == mime_type and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _extract_plain_text(payload: dict) -> str:
    """Prefers text/plain; falls back to text/html (stripped) when the
    message has no plain-text part at all. Some clients -- Yahoo Mail's
    webmail among them, confirmed 2026-09-13 on a real misclassified reply
    -- send a reply as HTML-only, with no text/plain alternative anywhere in
    the payload. Without this fallback, _find_part(..., "text/plain") comes
    back None, the message silently extracts to "", and classify_reply()
    receives an empty string -- reading as a genuinely blank reply rather
    than the lead's actual (often clearly interested) message, so it comes
    back "unclear" no matter what they said."""
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return plain
    html = _find_part(payload, "text/html")
    if html is not None:
        return _html_to_plain_text(html)
    return ""


def _message_to_reply(msg: dict) -> Optional[InboundReply]:
    """
    Shared by both poll_new_replies (per-thread full poll) and
    get_message_by_id (history.list's per-message fetch) so the two paths
    can never drift apart on what counts as a genuine reply -- same sender
    check, same quote-stripping, same HTML fallback, regardless of which
    mechanism found the message.
    """
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    from_header = headers.get("from", "")
    from_email = from_header.split("<")[-1].strip(">")
    if _is_our_own_address(from_email):
        return None
    body_text = _strip_quoted_reply(_extract_plain_text(msg["payload"]))
    received_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    return InboundReply(
        thread_id=msg["threadId"], from_email=from_email,
        body_text=body_text, message_id=msg["id"], received_at=received_at,
    )


def _resolve_threads(watched):
    return watched() if callable(watched) else watched


@dataclass
class PollResult:
    """
    New mail, plus a `commit` that saves the new position in the mailbox's
    history. Commit only AFTER the replies have been handled: if it were saved
    first, a failure partway through handling (an OpenAI error, a crash) would
    leave the position already past those replies, and they would never be seen
    again. Not committing just means the next poll sees them again, which is
    safe because handling a reply is idempotent (see tick.py).
    """
    replies: list[InboundReply]
    commit: Callable[[], None]


def begin_poll(gmail: GmailClientBase, state_store: PipelineStateStore, watched_thread_ids) -> PollResult:
    """
    The scalable way to find new replies: one history.list call covers new
    mail across the whole mailbox instead of one API call per active thread
    (348 calls -> ~1, at current scale). `watched_thread_ids` is a set, or a
    function returning one -- only the slow fallback path needs it, so a
    function lets the caller skip building it (a database read) on the normal
    fast path.

    Every failure mode here -- no cursor yet, an expired cursor, or genuinely
    anything else unexpected in the fast path -- falls back to the old,
    already-proven poll_new_replies rather than risk silently missing a reply.
    This function's job is choosing *how* to look for new mail; it is never the
    reason a reply goes undetected.
    """
    history_id = state_store.get_gmail_history_id()
    if history_id is None:
        logger.info("No Gmail history cursor yet -- doing one full poll, then bootstrapping.")
        return _full_poll(gmail, state_store, watched_thread_ids)

    try:
        new_messages, next_history_id = gmail.list_new_message_ids_since(history_id)
        replies = []
        for message_id, _thread_id in new_messages:
            # Deliberately NOT filtered to watched threads here -- see
            # run_pipeline_tick's sender-email fallback. A reply's thread_id can
            # legitimately not match what's stored on the lead: some mail clients
            # (confirmed live 2026-09-17, Yahoo Mail's webmail) don't set
            # In-Reply-To/References properly, so Gmail starts a brand-new thread
            # for a reply. The set of genuinely new messages each tick is small
            # (typically 0-10), so fetching all of them and letting the caller
            # match by thread-or-sender-email is a small, bounded cost.
            reply = gmail.get_message_by_id(message_id)
            if reply is not None:
                replies.append(reply)

        def commit() -> None:
            # Compare-and-swap against the value we actually read: if some other
            # tick already advanced the cursor since then, don't clobber their
            # progress with ours.
            if not state_store.set_gmail_history_id(next_history_id, expected_previous=history_id):
                logger.info(
                    "Gmail history cursor was already advanced by another tick before this one "
                    "finished -- skipping this write, not regressing it."
                )

        return PollResult(replies, commit)
    except Exception:  # noqa: BLE001 -- deliberately broad: ANY failure here must fall back, not just the documented expired-cursor case
        logger.exception(
            "history.list polling failed (expired cursor, transient API error, or something "
            "unanticipated) -- falling back to the full per-thread poll for this tick."
        )
        return _full_poll(gmail, state_store, watched_thread_ids)


def _full_poll(gmail: GmailClientBase, state_store: PipelineStateStore, watched_thread_ids) -> PollResult:
    # The position is read BEFORE polling: anything that arrives while we poll is
    # then at-or-after it, so the next poll sees it. (Reading it after would leave
    # a gap where mail arriving mid-poll was never seen by either poll.)
    baseline = gmail.get_history_id()
    replies = gmail.poll_new_replies(_resolve_threads(watched_thread_ids))

    def commit() -> None:
        try:
            state_store.set_gmail_history_id(baseline)
        except Exception:  # noqa: BLE001 -- losing the cursor bootstrap just means next tick tries again the same way
            logger.exception("Failed to bootstrap/refresh the Gmail history cursor; will retry next tick.")

    return PollResult(replies, commit)


def poll_new_replies_with_history_cursor(
    gmail: GmailClientBase, state_store: PipelineStateStore, watched_thread_ids,
) -> list[InboundReply]:
    """begin_poll with the position saved immediately -- for callers that don't handle replies afterwards."""
    result = begin_poll(gmail, state_store, watched_thread_ids)
    result.commit()
    return result.replies


_client: Optional[GmailClientBase] = None


def get_gmail_client() -> GmailClientBase:
    global _client
    if _client is None:
        _client = MockGmailClient() if settings.use_mock_integrations else LiveGmailClient()
    return _client
