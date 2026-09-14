"""Offline absolute exchange deadline tests; no native process or model calls."""

import json
from urllib.parse import urlparse

import httpx
import pytest

from claude_native_bridge import native
from claude_native_bridge.native import NativeSession
from claude_native_bridge.native_hooks import capture
from claude_native_bridge.settings import Settings


class FakeClock:
    def __init__(self):
        self.now = 10.0
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def monotonic_ns(self):
        return int(self.now * 1_000_000_000)

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DelayedHTTP:
    def __init__(self, session, clock, scripts):
        self.session = session
        self.clock = clock
        self.scripts = {path: list(entries) for path, entries in scripts.items()}
        self.calls = []

    def request(self, method, url, *, json=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append((path, timeout, self.clock.now))
        delay, payload = self.scripts[path].pop(0)
        if delay > timeout:
            self.clock.sleep(timeout)
            raise httpx.ReadTimeout("simulated deadline")
        self.clock.sleep(delay)
        if path == "/advance":
            capture(
                self.session.runtime,
                {
                    "session_id": self.session.session_id,
                    "prompt_id": "prompt-r",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": json["request"]["content"],
                },
            )
        return FakeResponse(payload)

    def close(self):
        return None


def make_session(tmp_path, clock, scripts, monkeypatch, timeout=0.1):
    session = NativeSession(
        Settings(request_timeout=timeout, retain_diagnostics=True),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=object(),
    )
    session.runtime = tmp_path
    session.port = 1234
    session.token = "fixture"
    http = DelayedHTTP(session, clock, scripts)
    session.http_client = http
    monkeypatch.setattr(native.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(native.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(native.time, "sleep", clock.sleep)
    monkeypatch.setattr(session, "close", lambda: setattr(session, "closed", True))
    return session, http


def test_delayed_submission_does_not_start_a_fresh_response_budget(tmp_path, monkeypatch):
    clock = FakeClock()
    session, http = make_session(
        tmp_path,
        clock,
        {
            "/advance": [(0.08, {"accepted": True})],
            "/response": [(0.03, {"response": None})],
        },
        monkeypatch,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        session.exchange("frame", "r")

    assert clock.now == pytest.approx(10.1)
    assert [call[0] for call in http.calls] == ["/advance", "/response"]
    assert http.calls[0][1] == pytest.approx(0.1)
    assert http.calls[1][1] == pytest.approx(0.02)


def test_delayed_status_uses_only_budget_remaining_after_submission_and_poll(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    session, http = make_session(
        tmp_path,
        clock,
        {
            "/advance": [(0.02, {"accepted": True})],
            "/response": [(0.02, {"response": None})],
            "/status": [(0.08, {"failed": None})],
        },
        monkeypatch,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        session.exchange("frame", "r")

    assert clock.now == pytest.approx(10.1)
    assert [call[0] for call in http.calls] == ["/advance", "/response", "/status"]
    assert http.calls[-1][1] == pytest.approx(0.06)


def test_usage_arriving_within_remaining_exchange_budget_is_retained(tmp_path, monkeypatch):
    clock = FakeClock()
    response = {
        "sequence": 1,
        "request_id": "r",
        "kind": "tool_calls",
        "tool_calls": [{"name": "safe", "arguments": {}}],
    }
    session, _ = make_session(
        tmp_path,
        clock,
        {
            "/advance": [(0.01, {"accepted": True})],
            "/response": [(0.04, {"response": response})],
        },
        monkeypatch,
    )

    def publish_usage():
        if clock.now >= 10.075 and not (tmp_path / "native-usage.json").exists():
            (tmp_path / "native-usage.json").write_text(
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "model": {"id": session.model},
                        "prompt_cache": {"requests": 1},
                        "context_window": {
                            "current_usage": {
                                "input_tokens": 11,
                                "output_tokens": 22,
                                "cache_creation_input_tokens": 33,
                                "cache_read_input_tokens": 44,
                            }
                        },
                        "captured_ns": clock.monotonic_ns(),
                    }
                )
            )

    clock.on_sleep = publish_usage

    assert session.exchange("frame", "r") == response
    assert clock.now == pytest.approx(10.075)
    assert session.last_usage.prompt_tokens == 88
    assert session._native_prompt_id == "prompt-r"


def test_late_optional_usage_cannot_extend_the_exchange_deadline(tmp_path, monkeypatch):
    clock = FakeClock()
    response = {
        "sequence": 1,
        "request_id": "r",
        "kind": "tool_calls",
        "tool_calls": [{"name": "safe", "arguments": {}}],
    }
    session, _ = make_session(
        tmp_path,
        clock,
        {
            "/advance": [(0.02, {"accepted": True})],
            "/response": [(0.06, {"response": response})],
        },
        monkeypatch,
    )

    assert session.exchange("frame", "r") == response
    assert clock.now == pytest.approx(10.1)
    assert session.last_usage is None
    assert session._native_prompt_id == "prompt-r"
