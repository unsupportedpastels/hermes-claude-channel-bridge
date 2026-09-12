"""Offline stale-native rebuild regressions; no native process or model calls."""

import json
from types import SimpleNamespace

import pytest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native import NativeSession, NativeSessionLost
from claude_native_bridge.protocol import HistoryTracker
from claude_native_bridge.settings import NativeBridgeError, Settings


class RebuildNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.fail_dead = False
        self.instances.append(self)

    def start(self):
        return self

    def health(self):
        return not self.closed

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(json.loads(content))
        if self.fail_dead:
            self.closed = True
            raise NativeBridgeError("native transport disappeared")
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def request(messages):
    return {
        "model": "claude-sonnet-5",
        "messages": messages,
        "extra_body": {"hermes_session_id": "rebuild-test"},
    }


def client(tmp_path, factory=RebuildNative):
    factory.instances = []
    return NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=factory,
    )


def test_dead_native_is_typed_and_next_request_rebuilds_from_canonical_history(tmp_path):
    bridge = client(tmp_path)
    initial = [{"role": "user", "content": "first"}]
    bridge.create(**request(initial))
    first = RebuildNative.instances[0]
    first.fail_dead = True
    following = initial + [
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
    ]

    with pytest.raises(NativeSessionLost, match="canonical history"):
        bridge.create(**request(following))

    assert not bridge.is_closed
    assert len(RebuildNative.instances) == 1  # uncertain request was not replayed

    bridge.create(**request(following))
    assert len(RebuildNative.instances) == 2
    rebuilt = RebuildNative.instances[1].frames[0]
    assert rebuilt["operation"] == "bootstrap"
    assert rebuilt["messages"] == following
    bridge.close()


def test_other_native_bridge_errors_keep_protective_client_close(tmp_path):
    class ConfigFailureNative(RebuildNative):
        def exchange(self, content, request_id, **kwargs):
            raise NativeBridgeError("authentication or configuration failed")

        def health(self):
            return True

    bridge = client(tmp_path, ConfigFailureNative)
    with pytest.raises(NativeBridgeError, match="authentication or configuration") as caught:
        bridge.create(**request([{"role": "user", "content": "first"}]))
    assert not isinstance(caught.value, NativeSessionLost)
    assert bridge.is_closed


def test_native_exchange_types_dead_process_but_not_live_transport_error(
    tmp_path, monkeypatch
):
    for process_alive in (False, True):
        session = NativeSession(
            Settings(request_timeout=0.1, retain_diagnostics=True),
            tmp_path,
            "claude-sonnet-5",
            "medium",
            http_client=object(),
        )
        session.runtime = tmp_path
        session.port = 1234
        session.token = "fixture"

        def transport_failure(*args, **kwargs):
            raise NativeBridgeError("transport failed")

        monkeypatch.setattr(session, "_api", transport_failure)
        monkeypatch.setattr(
            session,
            "_tmux",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0 if process_alive else 1
            ),
        )
        expected = NativeBridgeError if process_alive else NativeSessionLost
        with pytest.raises(expected) as caught:
            session.exchange("frame", "request-id")
        assert isinstance(caught.value, NativeSessionLost) is (not process_alive)
        assert session.closed


def test_hermes_tool_error_round_trips_without_history_reset():
    tracker = HistoryTracker()
    initial = [{"role": "user", "content": "run the guarded action"}]
    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_denied",
                "type": "function",
                "function": {"name": "guarded", "arguments": "{}"},
            }
        ],
    }
    tracker.commit(initial, None, None, tool_call)
    denial = {
        "role": "tool",
        "tool_call_id": "call_denied",
        "content": "Tool execution denied by user",
        "isError": True,
    }
    with_denial = initial + [tool_call, denial]

    prepared = tracker.prepare(with_denial, None)
    assert not prepared["reset"]
    assert json.loads(prepared["content"])["messages"] == [denial]

    answer = {"role": "assistant", "content": "The action was not run."}
    tracker.commit(with_denial, None, None, answer)
    continued = tracker.prepare(
        with_denial + [answer, {"role": "user", "content": "continue"}], None
    )
    assert not continued["reset"]
