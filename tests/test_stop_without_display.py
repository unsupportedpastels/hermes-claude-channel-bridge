"""Stop-only native completion coverage through exchange and HTTP SSE."""

import json

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native import NativeSession
from claude_native_bridge.native_hooks import capture
from claude_native_bridge.settings import NativeBridgeError, Settings

TOKEN = "test-only-stop-fallback-credential"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Hermes-Bridge-Client": "stop-fallback-owner",
}
MODEL = "claude-sonnet-5"


class StopOnlySession(NativeSession):
    """Real exchange lifecycle with fixture transport and native hook records."""

    instances = []
    mismatch_stop = False

    def __init__(self, settings, home, model, effort, **kwargs):
        super().__init__(settings, home, model, effort, **kwargs)
        self.runtime = self.home / ("stop-only-" + self.session_id)
        self.runtime.mkdir()
        self.responses = []
        self.instances.append(self)

    def start(self):
        return self

    def close(self):
        self.closed = True
        if self._owns_http_client:
            self.http_client.close()

    def health(self):
        return not self.closed

    def _collect_usage(self, baseline, started_ns, cancel_check=None, deadline=None):
        self.last_usage = None

    def _api(self, endpoint, payload=None, timeout=12, deadline=None):
        if endpoint == "/advance":
            assert payload is not None
            self.rid = payload["request"]["request_id"]
            self.prompt_id = "prompt-" + self.rid
            capture(
                self.runtime,
                {
                    "session_id": self.session_id,
                    "prompt_id": self.prompt_id,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": payload["request"]["content"],
                },
            )
            return {"accepted": True}
        if endpoint.startswith("/response?"):
            text = "FIRST_OK" if not self.responses else "SECOND_OK"
            if self.mismatch_stop:
                (self.runtime / "native-stop.json").write_text(
                    json.dumps(
                        {
                            "request_id": "mismatched-request",
                            "session_id": self.session_id,
                            "prompt_id": self.prompt_id,
                            "turn_id": None,
                            "event": "Stop",
                            "text": text,
                            "error": None,
                            "background_pending": False,
                            "generation": 1,
                        }
                    )
                )
            else:
                assert capture(
                    self.runtime,
                    {
                        "session_id": self.session_id,
                        "prompt_id": self.prompt_id,
                        "hook_event_name": "Stop",
                        "last_assistant_message": text,
                    },
                )
            return {"response": None}
        if endpoint == "/text-complete":
            assert payload is not None
            self.responses.append(payload["text"])
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
        raise AssertionError(endpoint)


def native_session(tmp_path):
    return StopOnlySession(
        Settings(development_channels_accepted=True), tmp_path, MODEL, "low"
    )


def test_stop_without_display_completes_exchange_and_session_remains_reusable(tmp_path):
    StopOnlySession.instances = []
    session = native_session(tmp_path)
    emitted = []

    try:
        first = session.exchange("first", "request-1", on_text=emitted.append)
        second = session.exchange("second", "request-2", on_text=emitted.append)

        assert first["text"] == "FIRST_OK"
        assert second["text"] == "SECOND_OK"
        assert session.responses == ["FIRST_OK", "SECOND_OK"]
        assert emitted == []
        assert not session.closed
    finally:
        session.close()


def test_mismatched_stop_without_display_is_rejected_and_session_closed(tmp_path):
    StopOnlySession.instances = []
    StopOnlySession.mismatch_stop = True
    session = native_session(tmp_path)

    try:
        with pytest.raises(NativeBridgeError, match="Uncorrelated native stop event"):
            session.exchange("first", "request-1")
        assert session.closed
        assert session.responses == []
    finally:
        StopOnlySession.mismatch_stop = False
        session.close()


def _sse_chunks(response):
    rows = [row[6:] for row in response.text.splitlines() if row.startswith("data: ")]
    assert rows[-1] == "[DONE]"
    return [json.loads(row) for row in rows[:-1]]


def _content(chunks):
    return [
        choice["delta"]["content"]
        for chunk in chunks
        for choice in chunk["choices"]
        if "content" in choice["delta"]
    ]


def test_stop_without_display_uses_final_remainder_over_actual_sse(tmp_path):
    StopOnlySession.instances = []
    clients = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        client = NativeBridgeClient(
            hermes_home=tmp_path,
            settings=Settings(development_channels_accepted=True),
            native_factory=StopOnlySession,
        )
        clients.append(client)
        return client

    app = create_app(TOKEN, tmp_path, factory)
    first_body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "first"}],
        "hermes_session_id": "stop-fallback-session",
        "stream": True,
    }
    second_body = {
        **first_body,
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "FIRST_OK"},
            {"role": "user", "content": "second"},
        ],
    }

    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=HEADERS, json=first_body)
        second = client.post("/v1/chat/completions", headers=HEADERS, json=second_body)

        assert first.status_code == second.status_code == 200
        assert _content(_sse_chunks(first)) == ["FIRST_OK"]
        assert _content(_sse_chunks(second)) == ["SECOND_OK"]
        assert len(StopOnlySession.instances) == 1
        native = StopOnlySession.instances[0]
        assert native.responses == ["FIRST_OK", "SECOND_OK"]
        assert not (native.runtime / "native-text.jsonl").exists()
        assert not native.closed

    assert len(clients) == 1
    assert native.closed
