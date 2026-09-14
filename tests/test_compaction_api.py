"""Offline compaction diagnostics through the real client and FastAPI surface."""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings

TOKEN = "test-only-compaction-credential"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Hermes-Bridge-Client": "compaction-owner",
}
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "show compaction diagnostics"}],
    "hermes_session_id": "compaction-session",
}
COMPLETED = {
    "status": "completed",
    "trigger": "auto",
    "request_id": "native-request-1",
    "active_request": True,
    "generation": 3,
    "summary_bytes": 2048,
    "error": None,
}
FAILED = {
    "status": "failed",
    "trigger": "manual",
    "request_id": None,
    "active_request": False,
    "generation": 4,
    "summary_bytes": None,
    "error": "correlation_mismatch",
}
INCOMPLETE = {
    "status": "compacting",
    "trigger": "auto",
    "request_id": "native-request-1",
    "active_request": True,
    "generation": 5,
    "summary_bytes": None,
    "error": "missing_end",
}
API_UNSET = object()


class OfflineCompactionNative:
    instances: ClassVar[list] = []
    diagnostic: ClassVar[dict | None] = None

    def __init__(self, settings, home, model, effort, **kwargs):
        self.session_id = f"native-compaction-{len(self.instances)}"
        self.runtime = Path(home) / self.session_id
        self.runtime.mkdir()
        self.last_usage = NS(prompt_tokens=11, completion_tokens=7, total_tokens=18)
        self.last_response_source = "respond"
        self.last_text = ""
        self.closed = False
        self.exchange_calls = 0
        self.instances.append(self)

    @property
    def last_compaction(self):
        if self.diagnostic is None:
            return None
        return dict(self.diagnostic)

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        self.exchange_calls += 1
        return {
            "sequence": self.exchange_calls,
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_native():
    OfflineCompactionNative.instances = []
    OfflineCompactionNative.diagnostic = None


def bridge_client(tmp_path):
    return NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=OfflineCompactionNative,
    )


def invoke(client):
    return cast(
        Any,
        client.chat.completions.create(
            model=BODY["model"],
            messages=BODY["messages"],
            extra_body={"hermes_session_id": BODY["hermes_session_id"]},
        ),
    )


@pytest.mark.parametrize("diagnostic", [COMPLETED, FAILED, INCOMPLETE, None])
def test_completion_exposes_only_safe_optional_compaction_projection(
    tmp_path, diagnostic
):
    OfflineCompactionNative.diagnostic = (
        None
        if diagnostic is None
        else {
            **diagnostic,
            "compact_summary": "private native summary",
            "transcript_path": "/private/native/transcript",
            "custom_instructions": "private instructions",
        }
    )
    client = bridge_client(tmp_path)
    try:
        completion = invoke(client)
    finally:
        client.close()

    if diagnostic is None:
        assert not hasattr(completion, "native_bridge_compaction")
    else:
        assert completion.native_bridge_compaction == diagnostic
        assert "private" not in json.dumps(completion.native_bridge_compaction)


def compaction_app(
    tmp_path, diagnostic, *, poison_api_value=False, api_override=API_UNSET
):
    OfflineCompactionNative.diagnostic = diagnostic
    engines = []

    class Engine:
        def __init__(self):
            self.client = bridge_client(tmp_path)
            self.chat = NS(completions=NS(create=self.create))
            self.create_calls = 0

        def create(self, **kwargs):
            self.create_calls += 1
            invoke_result = cast(Any, self.client.create(**kwargs))
            completion = invoke_result
            if poison_api_value and hasattr(completion, "native_bridge_compaction"):
                invoke_result.native_bridge_compaction.update(
                    compact_summary="private native summary",
                    transcript_path="/private/native/transcript",
                    hook_payload={"credential": "must-not-cross-api"},
                )
            if api_override is not API_UNSET:
                invoke_result.native_bridge_compaction = api_override
            return invoke_result

        def close(self):
            self.client.close()

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = Engine()
        engines.append(engine)
        return engine

    return create_app(TOKEN, tmp_path, factory), engines


def sse_chunks(response):
    rows = [row[6:] for row in response.text.splitlines() if row.startswith("data: ")]
    assert rows[-1] == "[DONE]"
    return [json.loads(row) for row in rows[:-1]]


@pytest.mark.parametrize("diagnostic", [COMPLETED, FAILED, INCOMPLETE, None])
def test_nonstream_api_and_cached_response_preserve_safe_optional_compaction(
    tmp_path, diagnostic
):
    app, engines = compaction_app(tmp_path, diagnostic, poison_api_value=True)

    with TestClient(app) as client:
        fresh = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        replay = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)

    assert fresh.status_code == replay.status_code == 200
    assert fresh.json() == replay.json()
    for response in (fresh, replay):
        payload = response.json()
        if diagnostic is None:
            assert "native_bridge_compaction" not in payload
        else:
            assert payload["native_bridge_compaction"] == diagnostic
        assert "private" not in response.text
        assert "must-not-cross-api" not in response.text
    assert engines[0].create_calls == 1
    assert OfflineCompactionNative.instances[0].exchange_calls == 1


@pytest.mark.parametrize("diagnostic", [COMPLETED, FAILED, INCOMPLETE, None])
@pytest.mark.parametrize("include_usage", [False, True])
def test_only_final_sse_chunk_exposes_compaction_on_fresh_and_cached_response(
    tmp_path, diagnostic, include_usage
):
    app, engines = compaction_app(tmp_path, diagnostic, poison_api_value=True)
    body: dict[str, Any] = dict(BODY, stream=True)
    if include_usage:
        body["stream_options"] = {"include_usage": True}

    with TestClient(app) as client:
        fresh = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        replay = client.post("/v1/chat/completions", headers=HEADERS, json=body)

    for response in (fresh, replay):
        chunks = sse_chunks(response)
        diagnostics = [
            chunk["native_bridge_compaction"]
            for chunk in chunks
            if "native_bridge_compaction" in chunk
        ]
        assert diagnostics == ([] if diagnostic is None else [diagnostic])
        if diagnostic is not None:
            assert chunks[-1]["native_bridge_compaction"] == diagnostic
        assert "private" not in response.text
        assert "must-not-cross-api" not in response.text
    assert engines[0].create_calls == 1
    assert OfflineCompactionNative.instances[0].exchange_calls == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {**COMPLETED, "generation": True},
        {**COMPLETED, "summary_bytes": -1},
        {**FAILED, "error": "private native failure details"},
        {**FAILED, "status": "unknown"},
        {**INCOMPLETE, "summary_bytes": 1},
        {**INCOMPLETE, "error": "correlation_mismatch"},
    ],
)
def test_api_omits_malformed_or_unbounded_compaction_metadata(tmp_path, invalid):
    app, _ = compaction_app(tmp_path, COMPLETED, api_override=invalid)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert "native_bridge_compaction" not in response.json()
    assert "private native failure details" not in response.text
