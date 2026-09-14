"""Offline provenance serialization through the real FastAPI surface."""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings
from claude_native_bridge.usage import usage_for_request


TOKEN = "test-only-provenance-credential"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Hermes-Bridge-Client": "provenance-owner",
}
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "show provenance"}],
    "hermes_session_id": "provenance-session",
}
EXPECTED_PROVENANCE = {
    "source": "native_status_line",
    "correlation_status": "correlated",
    "selected_model": "claude-sonnet-5",
    "observed_model": "claude-sonnet-5",
    "raw_counters": {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 33,
        "cache_read_input_tokens": 44,
    },
}


class OfflineEvidenceNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.model = model
        self.session_id = f"native-provenance-{len(self.instances)}"
        self.runtime = Path(home) / self.session_id
        self.runtime.mkdir()
        self.last_usage = None
        self.last_response_source = "respond"
        self.closed = False
        self.exchange_calls = 0
        self.instances.append(self)

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        self.exchange_calls += 1
        snapshot = {
            "session_id": self.session_id,
            "model": {"id": self.model},
            "prompt_cache": {"requests": 1},
            "context_window": {
                "current_usage": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "cache_creation_input_tokens": 33,
                    "cache_read_input_tokens": 44,
                }
            },
            "captured_ns": 1,
        }
        (self.runtime / "native-usage.json").write_text(json.dumps(snapshot))
        self.last_usage = usage_for_request(snapshot, self.session_id, self.model, 0)
        return {
            "sequence": self.exchange_calls,
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


class ClientEngine:
    """Use the real client, then inject fields the HTTP boundary must discard."""

    def __init__(self, home):
        self.create_calls = 0
        self.client = NativeBridgeClient(
            hermes_home=home,
            settings=Settings(development_channels_accepted=True),
            native_factory=OfflineEvidenceNative,
        )
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.create_calls += 1
        completion = cast(Any, self.client.create(**kwargs))
        completion.native_bridge_usage_provenance.update(
            billing_claim="must-not-cross-api",
            secret_payload={"credential": "must-not-cross-api"},
        )
        completion.native_bridge_usage_provenance["raw_counters"][
            "private_counter"
        ] = 999
        return completion

    def close(self):
        self.client.close()


def provenance_app(tmp_path):
    OfflineEvidenceNative.instances = []
    engines = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = ClientEngine(tmp_path)
        engines.append(engine)
        return engine

    return create_app(TOKEN, tmp_path, factory), engines


def sse_rows(response):
    return [row[6:] for row in response.text.splitlines() if row.startswith("data: ")]


def test_nonstream_preserves_only_safe_usage_provenance(tmp_path):
    app, engines = provenance_app(tmp_path)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)

    assert response.status_code == 200
    payload = response.json()
    assert payload["native_bridge_usage_provenance"] == EXPECTED_PROVENANCE
    assert "billing_claim" not in response.text
    assert "secret_payload" not in response.text
    assert "private_counter" not in response.text
    assert engines[0].create_calls == 1


@pytest.mark.parametrize("include_usage", [False, True])
def test_final_sse_chunk_preserves_safe_provenance_on_fresh_and_cached_replay(
    tmp_path, include_usage
):
    app, engines = provenance_app(tmp_path)
    body: dict[str, Any] = dict(BODY, stream=True)
    if include_usage:
        body["stream_options"] = {"include_usage": True}

    with TestClient(app) as client:
        fresh = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        replay = client.post("/v1/chat/completions", headers=HEADERS, json=body)

    for response in (fresh, replay):
        rows = sse_rows(response)
        assert rows[-1] == "[DONE]"
        chunks = [json.loads(row) for row in rows[:-1]]
        assert chunks[-1]["native_bridge_usage_provenance"] == EXPECTED_PROVENANCE
        assert all(
            "native_bridge_usage_provenance" not in chunk for chunk in chunks[:-1]
        )
        assert "billing_claim" not in response.text
        assert "secret_payload" not in response.text
        assert "private_counter" not in response.text

    assert engines[0].create_calls == 1
    assert OfflineEvidenceNative.instances[0].exchange_calls == 1
