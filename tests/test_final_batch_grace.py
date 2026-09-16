"""Offline regression: a debounced final display batch must not fail a request.

Claude Code emits display batches through a hook subprocess while the tool call
that yields the turn travels over its MCP pipe. The pipe wins the race, so the
journal can still be missing its end-of-message marker when the bridge reads it.
These tests pin the bounded wait that closes that race, and pin that the wait
stays bounded and strict when the marker genuinely never arrives.
"""

import json
import threading
import time
from types import SimpleNamespace as NS
from urllib.parse import urlparse

import pytest

from claude_native_bridge.native import (
    FINAL_BATCH_GRACE_SECONDS,
    NativeSession,
)
from claude_native_bridge.native_hooks import capture
from claude_native_bridge.settings import Settings

REQUEST_ID = "request-1"
PROMPT_ID = "prompt-1"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ScriptedHTTP:
    """Serves /advance and /response, binding the prompt like a real hook does."""

    def __init__(self, session, scripts, seed=True):
        self.session = session
        self.seed = seed
        self.scripts = {path: list(entries) for path, entries in scripts.items()}
        self.calls = []

    def request(self, method, url, *, json=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append(path)
        entry = self.scripts[path].pop(0)
        if isinstance(entry, Exception):
            raise entry
        if path == "/advance":
            capture(
                self.session.runtime,
                {
                    "session_id": self.session.session_id,
                    "prompt_id": PROMPT_ID,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "fixture",
                },
            )
            if self.seed:
                self.session.write_batch(0, False, "hello ")
        return FakeResponse(entry)

    def close(self):
        return None


def session_with_journal(tmp_path, monkeypatch, seed=True):
    session = NativeSession(
        Settings(development_channels_accepted=True, retain_diagnostics=True),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=object(),
    )
    session.runtime = tmp_path
    session.port = 1234
    session.token = "fixture"
    journal = tmp_path / "native-text.jsonl"

    def write_batch(index, final, delta):
        record = {
            "session_id": session.session_id,
            "request_id": REQUEST_ID,
            "prompt_id": PROMPT_ID,
            "turn_id": "turn-1",
            "message_id": "message-1",
            "index": index,
            "final": final,
            "delta": delta,
        }
        with journal.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    session.write_batch = write_batch
    session.http_client = ScriptedHTTP(
        session,
        {
            "/advance": [{"accepted": True}],
            "/response": [
                {
                    "response": {
                        "sequence": 1,
                        "request_id": REQUEST_ID,
                        "kind": "tool_calls",
                        "tool_calls": [{"name": "terminal", "arguments": {}}],
                    }
                }
            ],
        },
        seed=seed,
    )
    monkeypatch.setattr(session, "close", lambda: setattr(session, "closed", True))
    monkeypatch.setattr(
        session, "_tmux", lambda *a, **k: NS(returncode=1, stdout="", stderr="")
    )

    def append_final():
        if seed:
            write_batch(1, True, "world")
        else:
            write_batch(0, True, "hello world")

    return session, append_final


def test_late_final_display_batch_is_awaited_not_failed(tmp_path, monkeypatch):
    session, append_final = session_with_journal(tmp_path, monkeypatch)
    timer = threading.Timer(0.2, append_final)
    timer.start()
    try:
        response = session.exchange("frame", REQUEST_ID)
    finally:
        timer.join()

    assert response["kind"] == "tool_calls"
    assert session.last_text == "hello world"


def test_missing_final_display_batch_still_fails_bounded(tmp_path, monkeypatch):
    session, _ = session_with_journal(tmp_path, monkeypatch)
    started = time.monotonic()

    with pytest.raises(ValueError, match="Missing final native text batch"):
        session.exchange("frame", REQUEST_ID)

    assert time.monotonic() - started < 5


def test_response_before_the_first_display_batch_keeps_the_prose(tmp_path, monkeypatch):
    session, append_final = session_with_journal(tmp_path, monkeypatch, seed=False)
    # The status-line usage snapshot is a separate hook with its own bound; this
    # test is about the display journal, and the harness runs no status line.
    monkeypatch.setattr(session, "_collect_usage", lambda *a, **k: None)
    timer = threading.Timer(0.1, append_final)
    timer.start()
    try:
        response = session.exchange("frame", REQUEST_ID)
    finally:
        timer.join()

    assert response["kind"] == "tool_calls"
    assert session.last_text == "hello world"


def test_prose_less_yield_commits_empty_under_the_short_bound(tmp_path, monkeypatch):
    session, _ = session_with_journal(tmp_path, monkeypatch, seed=False)
    monkeypatch.setattr(session, "_collect_usage", lambda *a, **k: None)
    started = time.monotonic()
    response = session.exchange("frame", REQUEST_ID)
    elapsed = time.monotonic() - started

    assert response["kind"] == "tool_calls"
    assert session.last_text == ""
    assert elapsed < FINAL_BATCH_GRACE_SECONDS, (
        f"a prose-less yield must not pay the full grace ({elapsed:.2f}s)"
    )
