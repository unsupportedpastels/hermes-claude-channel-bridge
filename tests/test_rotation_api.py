"""Offline API projection for bridge-owned rotation diagnostics."""

import json
from types import SimpleNamespace as NS
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings


TOKEN = "test-only-rotation-credential"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Hermes-Bridge-Client": "rotation-owner",
}
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "rotate safely"}],
    "hermes_session_id": "rotation-session",
}
ROTATION = {
    "rotated": True,
    "reason": "context_tokens",
    "observed_tokens": 170_000,
    "threshold_tokens": 160_000,
    "window_tokens": 200_000,
    "window_source": "native_status_line",
    "native_exchanges": 3,
    "telemetry_exchange": 3,
}
INCOMING_ADMISSION = {
    "rotated": True,
    "reason": "incoming_admission",
    "observed_tokens": 159_950,
    "threshold_tokens": 160_000,
    "window_tokens": 200_000,
    "window_source": "native_status_line",
    "native_exchanges": 3,
    "telemetry_exchange": 3,
    "incoming_estimate": {
        "bytes": 51,
        "source": "utf8_bytes_conservative_bound",
        "native_tokens": None,
        "saturated": True,
    },
}
CHARACTER_CROSSING = {
    "rotated": True,
    "reason": "uncorrelated_usage_chars",
    "observed_chars": 599_900,
    "threshold_chars": 600_000,
    "native_exchanges": 3,
    "incoming_estimate": {
        "chars": 101,
        "source": "serialized_frame_chars",
        "native_tokens": None,
        "saturated": False,
    },
}


class Engine:
    def __init__(self, rotation):
        self.calls = 0
        self.rotation = rotation
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        return NS(
            id="chatcmpl-rotation",
            created=123,
            choices=[
                NS(
                    message=NS(role="assistant", content="done", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=NS(prompt_tokens=4, completion_tokens=1, total_tokens=5),
            native_bridge_rotation={
                **self.rotation,
                "private_runtime": "/private/native/runtime",
            },
            native_bridge_unexpected_compaction=True,
        )

    def close(self):
        pass


class ContextNative:
    instances: ClassVar[list] = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = home / f"native-{len(self.instances)}"
        self.last_context = None
        self.last_usage = None
        self.instances.append(self)

    def start(self):
        self.runtime.mkdir(parents=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        self.last_context = {"tokens": 159_950, "window": 200_000}
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def chunks(response):
    rows = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert rows[-1] == "[DONE]"
    return [json.loads(row) for row in rows[:-1]]


@pytest.mark.parametrize("rotation", [ROTATION, INCOMING_ADMISSION, CHARACTER_CROSSING])
def test_rotation_and_unexpected_compaction_cross_normal_cached_and_final_sse(
    tmp_path, rotation
):
    engines = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = Engine(rotation)
        engines.append(engine)
        return engine

    app = create_app(TOKEN, tmp_path, factory)
    with TestClient(app) as client:
        normal = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        cached = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        stream_body = dict(BODY, stream=True, stream_options={"include_usage": True})
        fresh_stream = client.post(
            "/v1/chat/completions", headers=HEADERS, json=stream_body
        )
        cached_stream = client.post(
            "/v1/chat/completions", headers=HEADERS, json=stream_body
        )

    for response in (normal, cached):
        assert response.status_code == 200
        assert response.json()["native_bridge_rotation"] == rotation
        assert response.json()["native_bridge_unexpected_compaction"] is True
        assert "private_runtime" not in response.text

    for response in (fresh_stream, cached_stream):
        parsed = chunks(response)
        carrying = [
            chunk
            for chunk in parsed
            if "native_bridge_rotation" in chunk
            or "native_bridge_unexpected_compaction" in chunk
        ]
        assert len(carrying) == 1
        assert carrying[0] is parsed[-1]
        assert carrying[0]["native_bridge_rotation"] == rotation
        assert carrying[0]["native_bridge_unexpected_compaction"] is True
        assert "private_runtime" not in response.text

    assert len(engines) == 1
    assert engines[0].calls == 2


def test_real_client_incoming_admission_metadata_crosses_sse_boundary(tmp_path):
    ContextNative.instances = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        return NativeBridgeClient(
            hermes_home=tmp_path,
            settings=Settings(development_channels_accepted=True),
            native_factory=ContextNative,
        )

    followup = dict(
        BODY,
        messages=[
            *BODY["messages"],
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "admit this next serialized frame"},
        ],
        stream=True,
    )
    app = create_app(TOKEN, tmp_path, factory)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        streamed = client.post("/v1/chat/completions", headers=HEADERS, json=followup)

    assert first.status_code == 200
    carrying = [
        chunk for chunk in chunks(streamed) if "native_bridge_rotation" in chunk
    ]
    assert len(carrying) == 1
    rotation = carrying[0]["native_bridge_rotation"]
    assert rotation == {
        "rotated": True,
        "reason": "incoming_admission",
        "observed_tokens": 159_950,
        "threshold_tokens": 160_000,
        "window_tokens": 200_000,
        "window_source": "native_status_line",
        "native_exchanges": 1,
        "telemetry_exchange": 1,
        "incoming_estimate": {
            "bytes": 51,
            "source": "utf8_bytes_conservative_bound",
            "native_tokens": None,
            "saturated": True,
        },
    }
    assert len(ContextNative.instances) == 2
    assert ContextNative.instances[0].closed
