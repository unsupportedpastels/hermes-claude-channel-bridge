"""Offline regression: a request that never reached the native is not a replay.

`Never replay an uncertain external action automatically` protects native input
that may already exist. A failure raised *before* the channel accepted the
request leaves no native input behind, so refusing the caller's identical retry
protects nothing and turns one transient failure into a dead turn. These tests
pin both sides of that line at the HTTP boundary.
"""

from types import SimpleNamespace as NS
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native import (
    NativeSession,
    NativeSessionLost,
    channel_environment,
)
from claude_native_bridge.settings import (
    NativeBridgeError,
    NativeRequestNotDelivered,
    Settings,
)

TOKEN = "test-only-delivery-credential"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Hermes-Bridge-Client": "owner-a"}
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "hello"}],
    "hermes_session_id": "session-a",
}


def completion(content="hello"):
    return NS(
        id="chatcmpl-delivery",
        object="chat.completion",
        created=123,
        model=BODY["model"],
        choices=[
            NS(
                index=0,
                message=NS(role="assistant", content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


class Engine:
    """First engine in a factory fails; later engines answer normally."""

    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.behavior is not None:
            self.behavior(self, kw)
        return completion()

    def close(self):
        pass


def app_factory(tmp_path, behavior=None):
    engines = []

    def factory(**kw):
        assert kw == {"hermes_home": tmp_path}
        engine = Engine(behavior if not engines else None)
        engines.append(engine)
        return engine

    return create_app(TOKEN, tmp_path, factory), engines


def undelivered(engine, kw):
    raise NativeRequestNotDelivered(
        "Native request was not delivered; the session was retired before any "
        "native input. A retry rebuilds from canonical history."
    )


def uncertain(engine, kw):
    raise RuntimeError("native outcome is unknown")


def _stream_rows(response):
    import json

    return [
        json.loads(row[6:])
        for row in response.text.splitlines()
        if row.startswith("data: ") and row != "data: [DONE]"
    ]


def _streamed_text(response):
    parts = []
    for chunk in _stream_rows(response):
        for choice in chunk.get("choices") or []:
            parts.append((choice.get("delta") or {}).get("content") or "")
    return "".join(parts)


@pytest.mark.parametrize("stream", [False, True])
def test_undelivered_failure_does_not_block_the_identical_retry(tmp_path, stream):
    app, engines = app_factory(tmp_path, undelivered)
    body = dict(BODY, stream=stream)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        if stream:
            assert '"error"' in first.text
            assert "[DONE]" not in first.text
        else:
            assert first.status_code == 502

        retry = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert retry.status_code == 200, retry.text
        if stream:
            assert "[DONE]" in retry.text
            assert _streamed_text(retry) == "hello"
        else:
            assert retry.json()["choices"][0]["message"]["content"] == "hello"
        assert len(engines) == 2
        assert len(engines[0].calls) == 1
        assert len(engines[1].calls) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", [uncertain, "session_lost"])
def test_delivered_failure_still_refuses_the_identical_retry(tmp_path, stream, failure):
    def behavior(engine, kw):
        if failure == "session_lost":
            raise NativeSessionLost(
                "Native session was lost; retry to rebuild from canonical history. "
                "The uncertain in-flight request was not replayed."
            )
        failure(engine, kw)

    app, engines = app_factory(tmp_path, behavior)
    body = dict(BODY, stream=stream)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        if stream:
            assert '"error"' in first.text
        else:
            assert first.status_code == 502

        retry = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert retry.status_code == 502
        assert "automatic replay refused" in retry.text
        assert len(engines) == 1
        assert len(engines[0].calls) == 1


def test_failure_log_names_the_class_and_branch_without_request_content(
    tmp_path, caplog
):
    app, engines = app_factory(tmp_path, undelivered)
    with caplog.at_level("WARNING", logger="claude_native_bridge.api"):
        with TestClient(app) as client:
            assert (
                client.post(
                    "/v1/chat/completions", headers=HEADERS, json=BODY
                ).status_code
                == 502
            )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "NativeRequestNotDelivered" in message and "not_delivered" in message
        for message in messages
    ), messages
    assert all("hello" not in message for message in messages)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ChannelStub:
    """Records which channel endpoints the engine actually called."""

    def __init__(self, scripts):
        self.scripts = {path: list(entries) for path, entries in scripts.items()}
        self.calls = []

    def request(self, method, url, *, json=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append(path)
        entry = self.scripts[path].pop(0)
        if isinstance(entry, Exception):
            raise entry
        return FakeResponse(entry)

    def close(self):
        return None


def native_session(tmp_path, scripts, monkeypatch):
    session = NativeSession(
        Settings(development_channels_accepted=True),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=object(),
    )
    session.runtime = tmp_path
    session.port = 1234
    session.token = "fixture"
    session.http_client = ChannelStub(scripts)
    monkeypatch.setattr(session, "close", lambda: setattr(session, "closed", True))
    monkeypatch.setattr(
        session, "_tmux", lambda *a, **k: NS(returncode=1, stdout="", stderr="")
    )
    return session


def test_failure_before_the_channel_accepts_is_tagged_not_delivered(
    tmp_path, monkeypatch
):
    session = native_session(tmp_path, {}, monkeypatch)
    (tmp_path / "native-attribution-error").touch(mode=0o600)

    with pytest.raises(NativeRequestNotDelivered):
        session.exchange("frame", "request-1")

    assert session.http_client.calls == []


def test_failure_after_delivery_stays_uncertain(tmp_path, monkeypatch):
    session = native_session(
        tmp_path,
        {
            "/advance": [{"accepted": True}],
            "/response": [
                {"response": {"sequence": 9, "request_id": "other", "kind": "final"}}
            ],
        },
        monkeypatch,
    )

    with pytest.raises(NativeBridgeError) as caught:
        session.exchange("frame", "request-2")

    assert not isinstance(caught.value, NativeRequestNotDelivered)
    assert session.http_client.calls == ["/advance", "/response"]


def test_channel_transport_failure_stays_uncertain(tmp_path, monkeypatch):
    session = native_session(
        tmp_path, {"/advance": [httpx.ConnectError("refused")]}, monkeypatch
    )

    with pytest.raises(NativeBridgeError) as caught:
        session.exchange("frame", "request-3")

    assert not isinstance(caught.value, NativeRequestNotDelivered)
    assert session.http_client.calls == ["/advance"]


def test_channel_diagnostics_are_opt_in():
    environment = channel_environment(Settings(), "/tmp/session-fixture")
    assert environment == {"HERMES_BRIDGE_RUNTIME_DIR": "/tmp/session-fixture"}
    assert channel_environment(
        Settings(channel_diagnostics=True), "/tmp/session-fixture"
    ) == {
        "HERMES_BRIDGE_RUNTIME_DIR": "/tmp/session-fixture",
        "HERMES_BRIDGE_DIAGNOSTICS": "1",
    }


class SetupFailureNative:
    """A native session whose launch fails before any request is delivered."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    def start(self):
        raise NativeBridgeError("native launch failed")

    def exchange(self, content, request_id, **kwargs):
        raise AssertionError("exchange must not run before a successful start")

    def health(self):
        return False

    def close(self):
        self.closed = True


def test_setup_failure_before_the_exchange_is_not_delivered(tmp_path):
    bridge = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=SetupFailureNative,
    )

    with pytest.raises(NativeRequestNotDelivered) as caught:
        bridge.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello"}],
            extra_body={"hermes_session_id": "session-a"},
        )

    assert "native launch failed" in str(caught.value)
    assert bridge.is_closed
