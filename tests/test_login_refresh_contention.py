"""Offline contract for native Claude login-refresh contention."""

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native_hooks import stopped_text
from claude_native_bridge.settings import (
    LOGIN_REFRESH_CONTENTION_CODE,
    LOGIN_REFRESH_CONTENTION_MESSAGE,
    NativeLoginRefreshContention,
    Settings,
)

TOKEN = "test-only-login-refresh-contention"
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "hello"}],
    "hermes_session_id": "login-refresh-session",
}


class FailingEngine:
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        raise self.failure

    def close(self):
        pass


def _headers(owner, *, lineage=True):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Hermes-Bridge-Client": owner,
    }
    if lineage:
        headers["X-Hermes-Bridge-Retry-Lineage"] = str(uuid.uuid4())
    return headers


def _error(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        return next(frame["error"] for frame in frames if "error" in frame)
    return response.json()["error"]


def test_native_hook_script_launch_captures_supported_event(tmp_path):
    """The hook must work under native.py's direct ``python native_hooks.py`` launch."""
    from claude_native_bridge.native_hooks import open_request

    open_request(tmp_path, "script-session", "script-request")
    hook = Path(__file__).resolve().parent.parent / "claude_native_bridge" / "native_hooks.py"
    payload = {
        "session_id": "script-session",
        "prompt_id": "script-prompt",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "request",
    }
    completed = subprocess.run(
        [sys.executable, str(hook), str(tmp_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    current = json.loads((tmp_path / "active-request.json").read_text(encoding="utf-8"))
    assert current["prompt_id"] == "script-prompt"
    assert not (tmp_path / "native-attribution-error").exists()


def test_stop_failure_is_typed_only_for_exact_login_refresh_contention():
    record = {
        "request_id": "r",
        "session_id": "s",
        "prompt_id": "p",
        "event": "StopFailure",
        "text": (
            "Could not refresh your login because another Claude Code process is refreshing it "
            "(or exited mid-refresh) · Try again in a minute; if it keeps happening, close other "
            "Claude Code windows or sign in again with /login"
        ),
        "error": "server_error",
        "background_pending": False,
    }
    with pytest.raises(NativeLoginRefreshContention) as caught:
        stopped_text(record, "r", "s", "p")
    assert str(caught.value) == LOGIN_REFRESH_CONTENTION_MESSAGE

    unknown = dict(record, text="different native failure")
    assert "Claude authentication needs attention" in str(caught.value)
    assert "credentials may be stale" in str(caught.value)
    assert "claude auth login" in str(caught.value)
    assert "machine running the bridge" in str(caught.value)
    assert "fresh /review" in str(caught.value)
    assert len(str(caught.value)) < 260
    with pytest.raises(ValueError, match="server_error"):
        stopped_text(unknown, "r", "s", "p")


def test_client_preserves_typed_terminal_failure_from_mock_native(tmp_path):
    class FailingNative:
        def __init__(self, *args, **kwargs):
            self.closed = False

        def start(self):
            return self

        def exchange(self, *args, **kwargs):
            raise NativeLoginRefreshContention()

        def health(self):
            return True

        def close(self):
            self.closed = True

    bridge = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=FailingNative,
    )
    with pytest.raises(NativeLoginRefreshContention) as caught:
        bridge.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello"}],
            extra_body={"hermes_session_id": "login-refresh-client"},
        )
    assert str(caught.value) == LOGIN_REFRESH_CONTENTION_MESSAGE
    assert bridge.is_closed


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("lineage", [False, True])
def test_http_and_repeated_request_preserve_sanitized_terminal_error(
    tmp_path, stream, lineage
):
    engines = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = FailingEngine(NativeLoginRefreshContention())
        engines.append(engine)
        return engine

    app = create_app(TOKEN, tmp_path, factory)
    headers = _headers("owner-stream" if stream else "owner-json", lineage=lineage)
    body = dict(BODY, stream=stream)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", headers=headers, json=body)
        repeated = client.post("/v1/chat/completions", headers=headers, json=body)

    assert first.status_code == (200 if stream else 502)
    assert repeated.status_code == 502
    for response in (first, repeated):
        error = _error(response)
        assert error == {
            "message": LOGIN_REFRESH_CONTENTION_MESSAGE,
            "type": "authentication_error",
            "code": LOGIN_REFRESH_CONTENTION_CODE,
        }
        assert "automatic replay refused" not in response.text
    assert len(engines) == 1
    assert engines[0].calls == 1


def _hermes_agent_repo():
    explicit = os.environ.get("HERMES_AGENT_REPO")
    if explicit:
        repo = Path(explicit).expanduser().resolve()
    else:
        try:
            spec = importlib.util.find_spec("agent.error_classifier")
        except (ImportError, ModuleNotFoundError):
            spec = None
        if spec is None or spec.origin is None:
            pytest.skip("Hermes host is not importable; set HERMES_AGENT_REPO")
        repo = Path(spec.origin).resolve().parents[1]
    if not (repo / "agent" / "error_classifier.py").is_file():
        pytest.fail(f"Invalid HERMES_AGENT_REPO: {repo}")
    return repo


def test_real_host_classifier_handles_openai_http_and_sse_exceptions(tmp_path):
    host = _hermes_agent_repo()
    source = """
import json
from unittest.mock import patch

import httpx
import openai
import agent.error_classifier as classifier
from claude_native_bridge.api_provider import make_profile
from claude_native_bridge.settings import (
    LOGIN_REFRESH_CONTENTION_CODE,
    LOGIN_REFRESH_CONTENTION_MESSAGE,
)

TERMINAL = {
    "message": LOGIN_REFRESH_CONTENTION_MESSAGE,
    "type": "authentication_error",
    "code": LOGIN_REFRESH_CONTENTION_CODE,
}
UNKNOWN = {
    "message": "opaque bridge failure",
    "type": "bridge_generation_error",
    "code": "generation_failed",
}


def sdk_exception(mode, envelope):
    def respond(request):
        payload = {"error": envelope}
        if mode == "http":
            return httpx.Response(502, json=payload, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(payload) + "\\n\\n").encode(),
            request=request,
        )

    client = openai.OpenAI(
        api_key="test-key",
        base_url="https://bridge.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    try:
        result = client.chat.completions.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello"}],
            stream=mode == "sse",
        )
        if mode == "sse":
            list(result)
    except Exception as exc:
        return exc
    raise AssertionError("Mock bridge error did not raise")


def classify(mode, envelope):
    exc = sdk_exception(mode, envelope)
    result = classifier.classify_api_error(
        exc,
        provider="claude-native-bridge",
        model="claude-sonnet-5",
    )
    return {
        "exception_type": type(exc).__name__,
        "exception_status": getattr(exc, "status_code", None),
        "reason": result.reason.value,
        "retryable": result.retryable,
        "rotate": result.should_rotate_credential,
        "fallback": result.should_fallback,
    }


with patch("providers.get_provider_profile", return_value=make_profile()):
    output = {
        "source": classifier.__file__,
        "http": classify("http", TERMINAL),
        "sse": classify("sse", TERMINAL),
        "unknown": classify("sse", UNKNOWN),
    }
print(json.dumps(output))
"""
    env = dict(os.environ)
    repo = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = os.pathsep.join((str(host), str(repo)))
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    classified = json.loads(completed.stdout)

    assert Path(classified["source"]).resolve() == (
        host / "agent" / "error_classifier.py"
    ).resolve()
    assert classified["http"] == {
        "exception_type": "InternalServerError",
        "exception_status": 502,
        "reason": "auth_permanent",
        "retryable": False,
        "rotate": False,
        "fallback": False,
    }
    assert classified["sse"] == {
        "exception_type": "APIError",
        "exception_status": None,
        "reason": "auth_permanent",
        "retryable": False,
        "rotate": False,
        "fallback": False,
    }
    assert classified["unknown"] == {
        "exception_type": "APIError",
        "exception_status": None,
        "reason": "unknown",
        "retryable": True,
        "rotate": False,
        "fallback": False,
    }
