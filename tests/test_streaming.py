import json
from typing import ClassVar

import pytest

from claude_native_bridge.client import NativeBridgeClient, assistant_dict
from claude_native_bridge.native import NativeSession
from claude_native_bridge.native_hooks import capture
from claude_native_bridge.settings import NativeBridgeError, Settings
from claude_native_bridge.streaming import TextBatches


class ScriptedSession(NativeSession):
    """Real exchange loop and hooks, fake transport/launcher only."""

    instances: ClassVar[list] = []
    scenario = "text"

    def __init__(self, settings, home, model, effort, **kwargs):
        super().__init__(settings, home, model, effort, **kwargs)
        self.runtime = home
        self.polls = 0
        self.advances = []
        self.instances.append(self)

    def start(self):
        return self

    def close(self):
        self.closed = True
        if self._owns_http_client:
            self.http_client.close()

    def _collect_usage(self, *args):
        self.last_usage = {"prompt_tokens": 3}

    def _api(self, endpoint, payload=None, **kwargs):
        if endpoint == "/advance":
            self.advances.append(payload)
            self.polls = 0
            self.rid = payload["request"]["request_id"]
            return {"accepted": True}
        if endpoint == "/text-complete":
            assert not self.closed
            return {
                "response": {
                    "sequence": self.sequence + 1,
                    "request_id": self.rid,
                    "kind": "final",
                    "text": payload["text"],
                }
            }
        if endpoint == "/status":
            return {"failed": None}
        self.polls += 1
        if self.polls == 1:
            event = batch(delta="Hello", final=True)
            event.update(
                session_id=self.session_id,
                request_id=self.rid,
                message_id="m-" + self.rid,
                hook_event_name="MessageDisplay",
            )
            capture(self.runtime, event)
            if self.scenario == "tool":
                return {
                    "response": {
                        "sequence": self.sequence + 1,
                        "request_id": self.rid,
                        "kind": "tool_calls",
                        "tool_calls": [{"name": "x", "arguments": {}}],
                    }
                }
        else:
            capture(
                self.runtime,
                {
                    "session_id": self.session_id,
                    "hook_event_name": "StopFailure"
                    if self.scenario == "failure"
                    else "Stop",
                    "error": "rate_limit" if self.scenario == "failure" else None,
                    "last_assistant_message": "conflict"
                    if self.scenario == "conflict"
                    else "Hello",
                },
            )
        return {"response": None}


def session(tmp_path, scenario="text"):
    s = ScriptedSession(
        Settings(development_channels_accepted=True), tmp_path, "claude-sonnet-5", "low"
    )
    s.scenario = scenario
    return s


def test_native_deltas_before_stop_and_same_lifecycle(tmp_path):
    s = session(tmp_path)
    received = []

    def on_text(delta):
        assert not (tmp_path / "native-stop.json").exists()
        received.append(delta)

    try:
        assert s.exchange("first", "r", on_text=on_text)["text"] == "Hello"
        assert not s.closed
        assert s.last_usage == {"prompt_tokens": 3}
        assert s.exchange("second", "next", on_text=on_text)["sequence"] == 2
        assert s.advances[1]["ack"] == 1
        assert received == ["Hello", "Hello"]
    finally:
        s.close()


@pytest.mark.parametrize("scenario", ["failure", "conflict"])
def test_native_incomplete_or_failed_closes(tmp_path, scenario):
    s = session(tmp_path, scenario)
    with pytest.raises((NativeBridgeError, ValueError)):
        s.exchange("first", "r")
    assert s.closed


def test_failure_arriving_with_tool_response_is_not_success(tmp_path):
    s = session(tmp_path, "tool")

    def usage_then_failure(*args):
        capture(
            tmp_path,
            {
                "session_id": s.session_id,
                "hook_event_name": "StopFailure",
                "error": "rate_limit",
            },
        )

    s._collect_usage = usage_then_failure
    with pytest.raises(ValueError, match="rate_limit"):
        s.exchange("first", "r")
    assert s.closed


def test_callback_exception_and_cancellation_close_native(tmp_path):
    s = session(tmp_path)

    def broken(delta):
        raise RuntimeError("closed downstream")

    with pytest.raises(RuntimeError):
        s.exchange("first", "r", on_text=broken)
    assert s.closed
    s = session(tmp_path)
    cancelled = []
    with pytest.raises(InterruptedError):
        s.exchange(
            "first", "r", on_text=cancelled.append, cancel_check=lambda: bool(cancelled)
        )
    assert s.closed


def test_pretool_content_commits_and_continues_history(tmp_path):
    ScriptedSession.instances = []
    c = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=ScriptedSession,
    )
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    messages = [{"role": "user", "content": "task"}]
    chunks = []
    ScriptedSession.scenario = "tool"
    try:
        result = c.create(
            model="claude-sonnet-5",
            messages=messages,
            tools=tools,
            extra_body={"hermes_session_id": "bound"},
            _on_text=chunks.append,
        )
        msg = assistant_dict(result)
        assert msg["content"] == "".join(chunks) == "Hello"
        messages += [
            msg,
            {
                "role": "tool",
                "tool_call_id": msg["tool_calls"][0]["id"],
                "content": "done",
            },
        ]
        c.create(
            model="claude-sonnet-5",
            messages=messages,
            tools=tools,
            extra_body={"hermes_session_id": "bound"},
        )
        assert len(ScriptedSession.instances) == 1
        frame = json.loads(
            ScriptedSession.instances[0].advances[1]["request"]["content"]
        )
        assert frame["operation"] == "continue"
    finally:
        ScriptedSession.scenario = "text"
        c.close()


def test_truncated_journal_cannot_finish(tmp_path):
    stream = TextBatches("s", "r")
    (tmp_path / "native-text.jsonl").write_text(
        json.dumps(batch(final=True)) + "\n" + '{"partial":'
    )
    stream.drain(tmp_path)
    with pytest.raises(ValueError):
        stream.finish("Hello")


def test_capture_limit_fails_closed_and_stale_message_rejected(tmp_path, monkeypatch):
    from claude_native_bridge import native_hooks

    (tmp_path / "active-request.json").write_text(
        json.dumps({"session_id": "s", "request_id": "r"})
    )
    monkeypatch.setattr(native_hooks, "MAX_CAPTURE_BYTES", 10)
    assert not capture(tmp_path, dict(batch(), hook_event_name="MessageDisplay"))
    with pytest.raises(ValueError):
        TextBatches("s", "r").drain(tmp_path)
    with pytest.raises(ValueError):
        TextBatches("s", "next", previous_messages={"m"}).add(
            dict(batch(), request_id="next")
        )


def batch(index=0, delta="Hello", final=False, **kw):
    return dict(
        session_id="s",
        request_id="r",
        turn_id="t",
        message_id="m",
        index=index,
        delta=delta,
        final=final,
        **kw,
    )


def test_order_dedup_and_reconcile():
    emitted = []
    stream = TextBatches("s", "r", emitted.append)
    stream.add(batch())
    stream.add(batch())
    stream.add(batch(1, " world\n", True))
    assert emitted == ["Hello", " world\n"]
    assert stream.finish("Hello world") == "Hello world\n"


@pytest.mark.parametrize(
    "change",
    [
        {"index": 1},
        {"session_id": "other"},
        {"request_id": "old"},
        {"index": True},
        {"delta": None},
        {"final": 1},
    ],
)
def test_invalid_batches_fail(change):
    stream = TextBatches("s", "r")
    record = batch()
    record.update(change)
    with pytest.raises(ValueError):
        stream.add(record)


def test_conflict_missing_final_and_turn_fail():
    stream = TextBatches("s", "r")
    stream.add(batch())
    with pytest.raises(ValueError):
        stream.add(batch(delta="different"))
    with pytest.raises(ValueError):
        stream.finish("Hello")
    record = batch(1)
    record["turn_id"] = "foreign"
    with pytest.raises(ValueError):
        stream.add(record)
    stream.add(batch(1, "", True))
    with pytest.raises(ValueError):
        stream.finish("different")
    assert stream.finish() == "Hello"  # pre-tool text, not a Stop


def test_plain_final_requires_batches_and_callback_errors_propagate():
    with pytest.raises(ValueError):
        TextBatches("s", "r").finish("unseen")

    def broken(delta):
        raise RuntimeError("consumer closed")

    with pytest.raises(RuntimeError):
        TextBatches("s", "r", broken).add(batch())
