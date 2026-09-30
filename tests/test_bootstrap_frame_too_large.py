"""Offline contract for a bootstrap whose unpageable part exceeds bootstrap_max_chars."""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.api_provider import _classify_bridge_error
from claude_native_bridge.client import NativeBridgeClient, _bounded_bootstrap
from claude_native_bridge.protocol import HistoryTracker
from claude_native_bridge.settings import (
    BOOTSTRAP_FRAME_TOO_LARGE_CODE,
    NativeBootstrapTooLarge,
    NativeRequestNotDelivered,
    Settings,
)

TOKEN = "test-only-bootstrap-frame-too-large"
COMMAND = "hermes config set claude_native_bridge.bootstrap_max_chars"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "t" * 400,
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
# A Kanban worker's opening request: preloaded skills in the system prompt, the
# tool inventory, and a one-line task. None of it can be paged out.
WORKER = [
    {"role": "system", "content": "s" * 600},
    {"role": "user", "content": "work kanban task t_1"},
]


class RecordingNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"run-{len(self.instances)}"
        self.instances.append(self)

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def _client(tmp_path, maximum):
    return NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True, bootstrap_max_chars=maximum
        ),
        native_factory=RecordingNative,
    )


def _create(client):
    return client.create(
        model="claude-sonnet-5",
        messages=WORKER,
        tools=TOOLS,
        extra_body={"hermes_session_id": "worker"},
    )


def test_first_frame_over_the_limit_reports_measured_size_and_the_fix(tmp_path):
    RecordingNative.instances = []
    client = _client(tmp_path, 500)
    try:
        with pytest.raises(NativeBootstrapTooLarge) as caught:
            _create(client)
    finally:
        client.close()

    error = caught.value
    assert isinstance(error, NativeRequestNotDelivered)
    assert error.maximum == 500
    assert error.required > 1_000
    message = str(error)
    assert COMMAND in message
    assert f"{error.required:,}" in message and "500" in message
    # The recovery command must survive the 200-character failure marker.
    assert COMMAND in message[:200]
    # Settings reject a limit that is not below rotation_fallback_chars.
    assert "rotation_fallback_chars" in message
    assert RecordingNative.instances[0].frames == []

    # The reported size is the real frame: that limit admits it, one less refuses.
    RecordingNative.instances = []
    admitted = _client(tmp_path, error.required)
    try:
        _create(admitted)
    finally:
        admitted.close()
    assert [len(frame) for frame in RecordingNative.instances[0].frames] == [
        error.required
    ]

    RecordingNative.instances = []
    refused = _client(tmp_path, error.required - 1)
    try:
        with pytest.raises(NativeBootstrapTooLarge):
            _create(refused)
    finally:
        refused.close()


def test_omission_notices_that_do_not_fit_report_the_smallest_frame(tmp_path):
    messages = [
        {"role": "system", "content": "s" * 300},
        {"role": "user", "content": "old " + "x" * 400},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "latest"},
    ]
    mandatory = [messages[0], messages[-1]]
    required = len(HistoryTracker().prepare(mandatory, None, None)["content"])
    native = NS(runtime=tmp_path)

    with pytest.raises(NativeBootstrapTooLarge) as caught:
        _bounded_bootstrap(messages, None, None, native, required + 10)

    assert caught.value.maximum == required + 10
    assert caught.value.required > required + 10
    assert not (tmp_path / "spool").exists()


class RefusingEngine:
    def __init__(self):
        self.calls = 0
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        raise NativeBootstrapTooLarge(200_928, 150_000)

    def close(self):
        pass


def _error(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        return next(frame["error"] for frame in frames if "error" in frame)
    return response.json()["error"]


def _request(stream):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Hermes-Bridge-Client": "owner-stream" if stream else "owner-json",
        "X-Hermes-Bridge-Retry-Lineage": str(uuid.uuid4()),
    }
    body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}],
        "hermes_session_id": "bootstrap-too-large",
        "stream": stream,
    }
    return headers, body


@pytest.mark.parametrize("stream", [False, True])
def test_http_names_the_limit_and_reevaluates_an_identical_retry(tmp_path, stream):
    engines = []

    def factory(**kwargs):
        engine = RefusingEngine()
        engines.append(engine)
        return engine

    app = create_app(TOKEN, tmp_path, factory)
    headers, body = _request(stream)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=headers, json=body)
        repeated = client.post("/v1/chat/completions", headers=headers, json=body)

    expected = {
        "message": str(NativeBootstrapTooLarge(200_928, 150_000)),
        "type": "invalid_request_error",
        "code": BOOTSTRAP_FRAME_TOO_LARGE_CODE,
    }
    for response in (first, repeated):
        assert response.status_code == (200 if stream else 400)
        assert _error(response) == expected
        assert "Native generation failed or disconnected" not in response.text
        assert "automatic replay refused" not in response.text
    # Nothing reached the native, so the retry is measured again rather than
    # tombstoned: raising the limit takes effect on the next attempt.
    assert sum(engine.calls for engine in engines) == 2


def test_default_sdk_retry_policy_sends_the_refused_request_once(tmp_path):
    """An SDK resends a 5xx by default, and each resend would start a native."""
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    app = create_app(TOKEN, tmp_path, lambda **kwargs: RefusingEngine())
    headers, body = _request(False)
    with TestClient(app) as client:
        refused = client.post("/v1/chat/completions", headers=headers, json=body)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            refused.status_code, json=refused.json(), request=request
        )

    sdk = openai.OpenAI(
        api_key="test-key",
        base_url="https://bridge.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(openai.BadRequestError):
        sdk.chat.completions.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello"}],
        )
    assert len(sent) == 1


def test_provider_classifies_only_the_exact_envelope_as_terminal():
    envelope = {
        "message": str(NativeBootstrapTooLarge(200_928, 150_000)),
        "type": "invalid_request_error",
        "code": BOOTSTRAP_FRAME_TOO_LARGE_CODE,
    }

    def classify(error):
        return _classify_bridge_error(
            None,
            status_code=None,
            error_code=error.get("code"),
            message=error.get("message"),
            body={"error": error},
            model="claude-sonnet-5",
        )

    assert classify(envelope) == {
        "reason": "unknown",
        "retryable": False,
        "should_rotate_credential": False,
        "should_fallback": False,
    }
    assert classify(dict(envelope, type="bridge_generation_error")) is None
    assert classify(dict(envelope, code="generation_failed")) is None
