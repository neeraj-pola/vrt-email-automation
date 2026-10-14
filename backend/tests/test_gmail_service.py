"""
Regression coverage for gmail_service._strip_quoted_reply and
_extract_plain_text.

Caught live: a reply's quoted trail contained our own footer text
("...Unsubscribe"), which the keyword-based unsubscribe detector then
matched, incorrectly marking a genuinely interested lead as unsubscribed.
"""
import base64
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.gmail_service import (
    HistoryExpiredError,
    LiveGmailClient,
    MockGmailClient,
    _extract_plain_text,
    _is_our_own_address,
    _strip_quoted_reply,
    poll_new_replies_with_history_cursor,
)
from app.services.storage_service import InMemoryPipelineStateStore


def _live_client_with_mock_service() -> LiveGmailClient:
    """A LiveGmailClient with its real __init__ (OAuth, network) skipped --
    only _service is real API surface, and that's replaced with a mock."""
    client = LiveGmailClient.__new__(LiveGmailClient)
    client._service = MagicMock()
    return client


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_strips_gmail_style_quote():
    raw = (
        "Yes I'm interested, evenings work best!\n\n"
        "On Mon, Aug 31, 2026 at 4:36 PM VRT Management Group, LLC <team@example.com> wrote:\n"
        "> Hi Neeraj,\n"
        "> ...\n"
        "> Don't want to hear from us again? Unsubscribe\n"
    )
    assert _strip_quoted_reply(raw) == "Yes I'm interested, evenings work best!"


def test_strips_outlook_style_quote():
    raw = (
        "Sounds great, let's talk Thursday.\n\n"
        "From: VRT Management Group, LLC <team@example.com>\n"
        "Sent: Monday, August 31, 2026\n"
        "Subject: Let's talk about growing your business\n\n"
        "Don't want to hear from us again? Unsubscribe"
    )
    assert _strip_quoted_reply(raw) == "Sounds great, let's talk Thursday."


def test_no_quote_returns_full_text():
    raw = "Not interested, thanks."
    assert _strip_quoted_reply(raw) == "Not interested, thanks."


def test_leading_angle_bracket_quote_stripped():
    raw = "Please unsubscribe me.\n\n> Don't want to hear from us again? Unsubscribe"
    # A genuine unsubscribe request in the lead's own words (not quoted)
    # must still be detected -- only the *quoted* copy is stripped.
    assert "unsubscribe" in _strip_quoted_reply(raw).lower()


def test_extract_plain_text_prefers_the_plain_part_when_both_exist():
    payload = {
        "mimeType": "multipart/alternative",
        "body": {"size": 0},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Yes, interested!")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>Yes, interested!</p>")}},
        ],
    }
    assert _extract_plain_text(payload) == "Yes, interested!"


def test_extract_plain_text_falls_back_to_html_when_no_plain_part_exists():
    """
    Caught live 2026-09-13: a lead's reply via Yahoo Mail's webmail arrived
    as HTML-only -- no text/plain part anywhere in the payload. The old
    _extract_plain_text only ever looked for text/plain, so it silently
    returned "" for a message that clearly said "I'm interested in a
    strategy consultation" -- classify_reply then read that empty string as
    "unclear" no matter what the lead actually wrote, and the pipeline
    auto-resent a follow-up right after she'd already replied.
    """
    payload = {
        "mimeType": "multipart/alternative",
        "body": {"size": 0},
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<div>I'm interested in a strategy consultation.</div>")}},
        ],
    }
    assert _extract_plain_text(payload) == "I'm interested in a strategy consultation."


def test_extract_plain_text_returns_empty_string_when_no_text_part_exists_at_all():
    payload = {"mimeType": "multipart/mixed", "body": {"size": 0}, "parts": []}
    assert _extract_plain_text(payload) == ""


def test_our_own_sent_message_is_never_read_as_a_reply(monkeypatch):
    """
    Caught live 2026-09-13: a whole batch of leads that had just gotten a
    follow-up showed up as "unclear reply" despite never having replied to
    anything. poll_new_replies had no check that a "reply" actually came
    from the lead rather than from our own mailbox -- our own just-sent
    follow-up landing in the thread was itself getting misread as an
    inbound reply, then classified (as "unclear", since it's marketing copy,
    not a real customer response) as if the lead had sent it.
    """
    monkeypatch.setattr(settings, "gmail_sender_address", "sales@vrtvega.com")
    assert _is_our_own_address("sales@vrtvega.com") is True
    assert _is_our_own_address("Rajesh <sales@vrtvega.com>".split("<")[-1].strip(">")) is True
    assert _is_our_own_address("SALES@VRTVEGA.COM") is True  # header casing shouldn't matter
    assert _is_our_own_address("realleadatyahoo@yahoo.com") is False


# ---------------------------------------------------------------------------
# poll_new_replies_with_history_cursor -- the scaling fix (2026-09-14): one
# history.list call for the whole mailbox instead of one poll_new_replies
# call per active thread. Every scenario here checks that a real reply is
# still found no matter which path (fast or fallback) actually handles it.
# ---------------------------------------------------------------------------

def test_cold_start_does_one_full_poll_then_bootstraps_a_cursor():
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    gmail.inject_reply("thread-1", "lead@example.com", "Yes I'm interested!")

    assert state.get_gmail_history_id() is None
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert [r.body_text for r in replies] == ["Yes I'm interested!"]
    assert state.get_gmail_history_id() is not None  # bootstrapped for next time


def test_incremental_poll_finds_a_new_reply_via_history_not_a_full_poll():
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    # Bootstrap first (as the real first tick would), with nothing pending.
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    gmail.inject_reply("thread-1", "lead@example.com", "Actually yes, let's talk.")
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert [r.body_text for r in replies] == ["Actually yes, let's talk."]


def test_incremental_poll_still_returns_mail_on_threads_we_are_not_watching():
    """
    Deliberately the opposite of what this test used to assert (2026-09-14
    -> 2026-09-17): watched_thread_ids is no longer used to filter here at
    all. A reply's thread_id can legitimately not match what's stored on a
    lead -- confirmed live: Yahoo Mail's webmail doesn't always set
    In-Reply-To/References on a reply, so Gmail starts a new thread for it
    even though a human clearly replied. Filtering here would make that
    reply undiscoverable forever, since it exists in no lead's thread_id to
    poll for. run_pipeline_tick (tick.py) now does the thread-or-sender-
    email matching instead, over the full set this function returns.
    """
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    gmail.inject_reply("thread-unrelated", "someone@example.com", "hi")
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert [r.thread_id for r in replies] == ["thread-unrelated"]


def test_expired_history_cursor_falls_back_to_full_poll_and_still_finds_the_reply():
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    gmail.inject_reply("thread-1", "lead@example.com", "Sorry, missed this -- yes interested.")
    gmail.raise_history_expired_once()
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert [r.body_text for r in replies] == ["Sorry, missed this -- yes interested."]
    assert state.get_gmail_history_id() is not None  # re-bootstrapped, not left stale


def test_any_unexpected_failure_in_the_fast_path_also_falls_back_to_full_poll(monkeypatch):
    """
    The fallback is deliberately not scoped to just the documented expired-
    cursor case -- staff asked explicitly that today's proven approach stays
    available if anything about the new path breaks in production, not only
    the one failure mode we anticipated.
    """
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    gmail.inject_reply("thread-1", "lead@example.com", "Still here, yes let's do it.")

    def _boom(history_id):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(gmail, "list_new_message_ids_since", _boom)
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert [r.body_text for r in replies] == ["Still here, yes let's do it."]


def test_our_own_sent_message_is_filtered_out_of_the_history_path_too(monkeypatch):
    """The sender check applies identically whichever path found the
    message -- see _message_to_reply, shared by both."""
    monkeypatch.setattr(settings, "gmail_sender_address", "sales@vrtvega.com")
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    # inject_reply always simulates a message *from* the given address --
    # simulate our own follow-up landing in the thread the same way.
    gmail.inject_reply("thread-1", "sales@vrtvega.com", "Just checking in!")
    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})

    assert replies == []


def test_multiple_replies_across_different_threads_all_found_in_one_poll():
    """A tick shouldn't need one reply per call -- a single history.list
    response can (and in a real burst, will) carry several leads' replies
    at once, on different threads."""
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1", "thread-2", "thread-3"})

    gmail.inject_reply("thread-1", "a@example.com", "Yes, interested")
    gmail.inject_reply("thread-2", "b@example.com", "No thanks")
    gmail.inject_reply("thread-3", "c@example.com", "Maybe, tell me more")

    replies = poll_new_replies_with_history_cursor(gmail, state, {"thread-1", "thread-2", "thread-3"})

    assert {r.thread_id for r in replies} == {"thread-1", "thread-2", "thread-3"}
    assert {r.body_text for r in replies} == {"Yes, interested", "No thanks", "Maybe, tell me more"}


def test_concurrent_cursor_updates_do_not_regress_each_other():
    """
    Nothing stops the auto-tick and a manual "Check replies" click from
    overlapping, or one auto-tick cycle overrunning into the next. Both
    would read the same starting cursor; whichever finishes last must not
    blindly overwrite the other's advance with stale data, or the next tick
    re-reads from an earlier point than it should.
    """
    gmail = MockGmailClient()
    state = InMemoryPipelineStateStore()
    poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})  # bootstrap

    # "Tick A" reads the cursor and computes its own next value, but is slow
    # to actually write it (imagine an LLM call for classification in between).
    history_before_a = state.get_gmail_history_id()
    gmail.inject_reply("thread-1", "lead@example.com", "first reply")
    _, next_a = gmail.list_new_message_ids_since(history_before_a)

    # "Tick B" reads the same starting point, but runs all the way through
    # first -- including finding a second reply that arrived meanwhile.
    gmail.inject_reply("thread-1", "lead@example.com", "second reply")
    replies_b = poll_new_replies_with_history_cursor(gmail, state, {"thread-1"})
    assert [r.body_text for r in replies_b] == ["first reply", "second reply"]
    cursor_after_b = state.get_gmail_history_id()

    # Tick A finally tries to write its now-stale cursor -- must be rejected.
    wrote = state.set_gmail_history_id(next_a, expected_previous=history_before_a)
    assert wrote is False
    assert state.get_gmail_history_id() == cursor_after_b  # untouched by A's stale write


def test_list_new_message_ids_since_paginates_across_multiple_pages():
    """If a lot happens between ticks (e.g. after downtime), history.list's
    response is paginated -- one page must not silently drop the rest."""
    client = _live_client_with_mock_service()
    page1 = {
        "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
        "nextPageToken": "page2",
        "historyId": "101",
    }
    page2 = {
        "history": [{"messagesAdded": [{"message": {"id": "m2", "threadId": "t2"}}]}],
        "historyId": "102",
    }
    client._service.users.return_value.history.return_value.list.return_value.execute.side_effect = [page1, page2]

    found, next_id = client.list_new_message_ids_since("100")

    assert found == [("m1", "t1"), ("m2", "t2")]
    assert next_id == "102"  # the *last* page's historyId, not the first
    assert client._service.users.return_value.history.return_value.list.call_count == 2


def test_list_new_message_ids_since_raises_history_expired_on_404():
    """Gmail only retains history for a limited window -- a stale enough
    cursor comes back 404, which must surface as the specific exception the
    orchestrator knows to catch and fall back on, not an opaque HttpError."""
    from googleapiclient.errors import HttpError

    client = _live_client_with_mock_service()
    mock_resp = MagicMock(status=404)
    error = HttpError(resp=mock_resp, content=b'{"error": {"message": "Not Found"}}')
    client._service.users.return_value.history.return_value.list.return_value.execute.side_effect = error

    with pytest.raises(HistoryExpiredError):
        client.list_new_message_ids_since("100")


def test_get_message_by_id_returns_none_when_the_message_has_vanished():
    """A message can be deleted/trashed in the gap between history.list
    listing it and us fetching its full content -- must not crash the tick,
    just skip that one message."""
    client = _live_client_with_mock_service()
    client._service.users.return_value.messages.return_value.get.return_value.execute.side_effect = \
        Exception("404: message not found")

    assert client.get_message_by_id("some-vanished-id") is None
