"""Ingress path: Hermes gates, loopback API validation, and teardown privacy."""

from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import MAX_BODY_BYTES, create_app
from claude_native_bridge.api_provider import make_profile
from claude_native_bridge.images import externalize_images
from claude_native_bridge.native import NativeSession
from claude_native_bridge.settings import Settings
from claude_native_bridge.supervisor import _archive_run

from image_fixtures import PNG, image_part

TOKEN = "test-only-local-credential"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Hermes-Bridge-Client": "owner-a"}


def test_profile_declares_both_vision_gates_and_hermes_accepts_tool_images():
    profile = make_profile()
    assert profile.supports_vision is True
    assert profile.supports_vision_tool_messages is True
    providers = pytest.importorskip("providers")
    vision = pytest.importorskip("tools.vision_tools")
    providers.register_provider(profile)
    assert vision._profile_rejects_tool_media(profile.name, "claude-sonnet-5") is False
    assert vision._supports_media_in_tool_results(profile.name, "claude-sonnet-5") is True


class Engine:
    def __init__(self):
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return NS(
            id="chatcmpl-test", object="chat.completion", created=1, model="claude-sonnet-5",
            choices=[NS(index=0, message=NS(role="assistant", content="seen", tool_calls=None), finish_reason="stop")],
            usage=None,
        )

    def close(self):
        pass


def app_and_engines(tmp_path):
    engines = []

    def factory(**kwargs):
        engines.append(Engine())
        return engines[-1]

    return create_app(TOKEN, tmp_path, factory), engines


def body(content, role="user"):
    messages = [{"role": "user", "content": "hi"}] if role == "tool" else []
    if role == "tool":
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c", "content": content},
        ]
    else:
        messages.append({"role": role, "content": content})
    return {"model": "claude-sonnet-5", "messages": messages, "hermes_session_id": "s",
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {}}}]}


@pytest.mark.parametrize("role", ["user", "tool"])
def test_api_passes_valid_images_to_the_engine_unchanged(tmp_path, role):
    app, engines = app_and_engines(tmp_path)
    request = body([{"type": "text", "text": "look"}, image_part()], role)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=request)
    assert response.status_code == 200
    assert engines[0].calls[0]["messages"] == request["messages"]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
        {"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,PHN2Zz4="}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,####"}},
    ],
)
def test_api_rejects_unsupported_images_naming_that_they_were_not_seen(tmp_path, part):
    app, engines = app_and_engines(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=body([part]))
    assert response.status_code == 400
    assert "not seen" in response.json()["detail"]
    assert not engines or not engines[0].calls


def test_api_still_rejects_other_unsupported_media_generically(tmp_path):
    app, _ = app_and_engines(tmp_path)
    request = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x", "audio": {"id": "a"}}]}
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=request)
    assert response.status_code == 400


def test_whole_request_over_8_mib_is_rejected_not_truncated(tmp_path):
    app, engines = app_and_engines(tmp_path)
    huge = PNG + b"x" * (MAX_BODY_BYTES * 3 // 4)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=body([image_part(huge)]))
    assert response.status_code == 413
    assert not engines or not engines[0].calls


def native_with_images(tmp_path, retain):
    session = NativeSession(
        Settings(retain_diagnostics=retain), tmp_path, "claude-sonnet-5", "medium",
        http_client=NS(close=lambda: None),
    )
    session.runtime = tmp_path / "run"
    session.runtime.mkdir()
    (session.runtime / "diagnostic.txt").write_text("kept")
    externalize_images([{"role": "user", "content": [image_part()]}], session.runtime)
    assert any((session.runtime / "images").iterdir())
    return session


@pytest.mark.parametrize("retain", [False, True])
def test_native_close_never_retains_image_blobs(tmp_path, retain):
    session = native_with_images(tmp_path, retain)

    session.close()

    assert not (session.runtime / "images").exists()
    assert (session.runtime / "diagnostic.txt").exists() is retain


def test_archived_orphan_runs_drop_image_blobs(tmp_path):
    runtime = tmp_path / "session-x"
    runtime.mkdir()
    (runtime / "note").write_text("n")
    externalize_images([{"role": "user", "content": [image_part()]}], runtime)

    archived = _archive_run(runtime)

    assert (archived / "note").exists() and not (archived / "images").exists()
