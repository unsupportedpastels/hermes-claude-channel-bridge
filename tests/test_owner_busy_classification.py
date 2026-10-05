"""Host-classifier contract for the bridge's owner-busy admission refusal.

An interrupt-and-redirect makes Hermes abort the live stream and reopen one
within milliseconds, while the bridge is still retiring the aborted request.
The bridge answers ``409 Owner already has an active request; no inference
started``. No inference ran, so the caller may retry after a backoff; treating
it as a terminal client error sends the whole turn to the fallback provider.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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


def test_real_host_classifier_retries_owner_busy_without_fallback(tmp_path):
    host = _hermes_agent_repo()
    source = """
import json
from unittest.mock import patch

import httpx
import openai
import agent.error_classifier as classifier
from claude_native_bridge.api_provider import make_profile

BUSY = "Owner already has an active request; no inference started"


def sdk_exception(status, payload):
    def respond(request):
        return httpx.Response(status, json=payload, request=request)

    client = openai.OpenAI(
        api_key="test-key",
        base_url="https://bridge.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    try:
        client.chat.completions.create(
            model="claude-opus-5-5",
            messages=[{"role": "user", "content": "hello"}],
        )
    except Exception as exc:
        return exc
    raise AssertionError("Mock bridge error did not raise")


def classify(status, payload):
    exc = sdk_exception(status, payload)
    result = classifier.classify_api_error(
        exc, provider="claude-native-bridge", model="claude-opus-5-5"
    )
    return {
        "exception_type": type(exc).__name__,
        "exception_status": getattr(exc, "status_code", None),
        "retryable": result.retryable,
        "rotate": result.should_rotate_credential,
        "fallback": result.should_fallback,
    }


with patch("providers.get_provider_profile", return_value=make_profile()):
    output = {
        "source": classifier.__file__,
        "busy": classify(409, {"detail": BUSY}),
        "other_409": classify(409, {"detail": "something else"}),
        "replay": classify(
            502,
            {"detail": "Previous identical request failed; automatic replay refused"},
        ),
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
    busy = classified["busy"]
    assert busy["exception_type"] == "ConflictError"
    assert busy["exception_status"] == 409
    assert busy["retryable"] is True
    assert busy["fallback"] is False
    assert busy["rotate"] is False
    # Only the exact owner-busy refusal is recognised; a different 409 and the
    # uncertain-replay refusal keep the host's own handling.
    assert classified["other_409"]["retryable"] is False
    assert classified["replay"] != busy
