"""Offline HTTP contract tests: no native inference or persistent config."""

import asyncio
import json
import threading
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_native_bridge.api import create_app
from claude_native_bridge.models import MODELS

TOKEN = "test-only-local-credential"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Hermes-Bridge-Client": "owner-a"}
BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "hello"}],
    "hermes_session_id": "session-a",
}


def completion(content="hello", calls=None, usage=None, finish="stop"):
    return NS(
        id="chatcmpl-test",
        object="chat.completion",
        created=123,
        model=BODY["model"],
        choices=[
            NS(
                index=0,
                message=NS(role="assistant", content=content, tool_calls=calls),
                finish_reason=finish,
            )
        ],
        usage=usage,
    )


class Engine:
    def __init__(self, behavior=None):
        self.calls = []
        self.closed = threading.Event()
        self.behavior = behavior
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return self.behavior(self, kw) if self.behavior else completion()

    def close(self):
        self.closed.set()


def app_factory(tmp_path, behavior=None, owner_limit=None):
    engines = []

    def factory(**kw):
        assert kw == {"hermes_home": tmp_path}
        engine = Engine(behavior)
        engines.append(engine)
        return engine

    return create_app(
        TOKEN, tmp_path, factory, owner_limit=owner_limit
    ), engines


def test_auth_catalog_and_ordinary_completion(tmp_path):
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:
        for url in ("/health", "/v1/models"):
            assert client.get(url).status_code == 401
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401
        assert client.get("/health", headers=HEADERS).status_code == 200
        models = client.get("/v1/models", headers=HEADERS).json()
        assert {m["id"] for m in models["data"]} == set(MODELS)
        assert not engines
        result = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        assert result.status_code == 200
        data = result.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "hello"
        assert data["usage"] is None
        assert engines[0].calls[0]["extra_body"] == {"hermes_session_id": "session-a"}
        assert engines[0].calls[0]["stream"] is False
    assert engines[0].closed.is_set()


def test_title_response_format_is_validated_then_stripped_for_native(tmp_path):
    app, engines = app_factory(tmp_path)
    body = dict(
        BODY,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "title",
                "schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
            },
        },
    )
    with TestClient(app) as client:
        result = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert result.status_code == 200
        assert "response_format" not in engines[0].calls[0]
        for invalid in ("json", {}, {"type": "xml"}):
            rejected = client.post(
                "/v1/chat/completions",
                headers=dict(HEADERS, **{"X-Hermes-Bridge-Client": "invalid"}),
                json=dict(BODY, response_format=invalid),
            )
            assert rejected.status_code == 400


def test_explicit_owner_close_releases_native_and_is_idempotent(tmp_path):
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
        assert not engines[0].closed.is_set()
        assert client.post("/v1/owner/close").status_code == 401
        result = client.post("/v1/owner/close", headers=HEADERS)
        assert result.status_code == 200
        assert result.json() == {"closed": True}
        assert engines[0].closed.is_set()
        assert client.post("/v1/owner/close", headers=HEADERS).json() == {"closed": False}


def test_configured_owner_capacity_is_global_and_close_frees_slot(tmp_path):
    app, engines = app_factory(tmp_path, owner_limit=1)
    owner_b = dict(HEADERS, **{"X-Hermes-Bridge-Client": "owner-b"})
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
        blocked = client.post("/v1/chat/completions", headers=owner_b, json=BODY)
        assert blocked.status_code == 429
        assert len(engines) == 1
        assert client.post("/v1/owner/close", headers=HEADERS).json() == {"closed": True}
        assert client.post("/v1/chat/completions", headers=owner_b, json=BODY).status_code == 200
        assert len(engines) == 2


def test_duplicates_owner_and_missing_binding_isolation(tmp_path):
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:

        def post(body=BODY, headers=HEADERS):
            return client.post("/v1/chat/completions", headers=headers, json=body)

        assert post().json() == post().json()
        assert len(engines) == 1 and len(engines[0].calls) == 1
        assert post(dict(BODY, hermes_session_id="other")).status_code == 200
        assert len(engines) == 1 and len(engines[0].calls) == 2
        assert (
            post(
                headers=dict(HEADERS, **{"X-Hermes-Bridge-Client": "owner-b"})
            ).status_code
            == 200
        )
        assert len(engines) == 2
        unbound = {k: v for k, v in BODY.items() if k != "hermes_session_id"}
        for _ in range(2):
            assert post(unbound).status_code == 200
            assert (
                post(headers={"Authorization": HEADERS["Authorization"]}).status_code
                == 200
            )
        assert len(engines) == 6
        assert all(e.closed.is_set() for e in engines[2:])


@pytest.mark.parametrize(
    "change",
    [
        {"model": "arbitrary"},
        {"model": []},
        {"messages": "prompt"},
        {"messages": [{"role": "user", "content": 12}]},
        {"stream": "yes"},
        {"tools": "execute"},
        {"_on_text": "anything"},
        {"extra_body": {}},
        {"native_factory": "anything"},
        {"hermes_session_id": 123},
        {"stream_options": {"include_usage": "yes"}},
        {"n": 2},
        {"temperature": True},
        {"max_tokens": -1},
    ],
)
def test_invalid_input_never_invokes(tmp_path, change):
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=HEADERS, json=dict(BODY, **change)
            ).status_code
            == 400
        )
        assert not engines


def test_oversized_result_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_native_bridge.api.MAX_BODY_BYTES", 512)
    app, engines = app_factory(tmp_path, lambda engine, kw: completion("x" * 1024))
    with TestClient(app) as client:
        result = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
        assert result.status_code == 502
        assert engines[0].closed.is_set()


def test_body_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_native_bridge.api.MAX_BODY_BYTES", 64)
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=HEADERS, content=b" " * 65
            ).status_code
            == 413
        )
        assert not engines


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
]
CALLS = [
    NS(
        id="call-1",
        type="function",
        function=NS(name="lookup", arguments='{"q":"hello"}'),
    )
]
USAGE = NS(prompt_tokens=11, completion_tokens=7, total_tokens=18)


def test_tools_usage_and_sse_no_duplicate_text(tmp_path):
    def behavior(engine, kw):
        if "_on_text" in kw:
            kw["_on_text"]("he")
            kw["_on_text"]("llo")
        return completion("hello!", CALLS, USAGE, "tool_calls")

    app, engines = app_factory(tmp_path, behavior)
    with TestClient(app) as client:
        body = dict(BODY, tools=TOOLS)
        data = client.post("/v1/chat/completions", headers=HEADERS, json=body).json()
        assert data["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        assert (
            data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            == '{"q":"hello"}'
        )
        body.update(stream=True, stream_options={"include_usage": True})
        response = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        rows = [
            row[6:] for row in response.text.splitlines() if row.startswith("data: ")
        ]
        assert rows[-1] == "[DONE]"
        chunks = [json.loads(row) for row in rows[:-1]]
        deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
        assert "".join(d.get("content", "") for d in deltas) == "hello!"
        calls = [d["tool_calls"] for d in deltas if "tool_calls" in d]
        assert len(calls) == 1 and calls[0][0]["index"] == 0
        assert chunks[-1]["usage"] == data["usage"]
        assert len({c["id"] for c in chunks}) == 1
        replay = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        assert "[DONE]" in replay.text
        assert len(engines[0].calls) == 2


@pytest.mark.parametrize(
    "failure,stream",
    [
        (failure, stream)
        for failure in ("exception", "incomplete", "bad-tool", "divergence")
        for stream in (False, True)
        if failure != "divergence" or stream
    ],
)
def test_errors_cleanup_and_no_success(tmp_path, stream, failure):
    def behavior(engine, kw):
        if failure == "exception":
            raise RuntimeError("private prompt and credential must not leak")
        if failure == "incomplete":
            return completion("truncated", finish="length")
        if failure == "bad-tool":
            return completion(
                None,
                [
                    NS(
                        id="call",
                        type="function",
                        function=NS(name="lookup", arguments='{"q":5}'),
                    )
                ],
                finish="tool_calls",
            )
        if "_on_text" in kw:
            kw["_on_text"]("wrong")
        return completion("different")

    app, engines = app_factory(tmp_path, behavior)
    with TestClient(app) as client:
        body = dict(BODY, stream=stream, tools=TOOLS)
        result = client.post("/v1/chat/completions", headers=HEADERS, json=body)
        if stream:
            assert '"error"' in result.text
            assert "[DONE]" not in result.text
            assert '"finish_reason": "stop"' not in result.text
        else:
            assert result.status_code == 502
        assert "private prompt" not in result.text
        assert engines[0].closed.is_set()
        assert (
            client.post("/v1/chat/completions", headers=HEADERS, json=body).status_code
            == 502
        )
        assert len(engines) == 1


async def asgi_request(app, body, headers=HEADERS, fail_send=False):
    incoming = asyncio.Queue()
    await incoming.put(
        {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    )
    messages = []
    text_seen = asyncio.Event()

    async def send(message):
        if fail_send == "start":
            raise OSError("socket closed before response headers")
        messages.append(message)
        if b'"content": "early"' in message.get("body", b""):
            text_seen.set()
            if fail_send:
                raise OSError("socket closed")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4" if fail_send else "2.3"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 1000),
        "http_version": "1.1",
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    return task, incoming, messages, text_seen


def test_sse_arrives_before_engine_completion_and_conflict(tmp_path):
    release = threading.Event()

    def behavior(engine, kw):
        kw["_on_text"]("early")
        assert release.wait(3)
        return completion("early final")

    app, engines = app_factory(tmp_path, behavior)

    async def run():
        async with app.router.lifespan_context(app):
            task, incoming, messages, seen = await asgi_request(
                app, dict(BODY, stream=True)
            )
            try:
                await asyncio.wait_for(seen.wait(), 2)
                assert not task.done() and not release.is_set()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client:
                    for body in (
                        dict(BODY, stream=True),
                        dict(BODY, messages=[{"role": "user", "content": "different"}]),
                    ):
                        result = await client.post(
                            "/v1/chat/completions", headers=HEADERS, json=body
                        )
                        assert result.status_code == 409
                assert len(engines) == 1 and len(engines[0].calls) == 1
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
            assert b"[DONE]" in b"".join(m.get("body", b"") for m in messages)

    asyncio.run(run())


def test_openai_sdk_json_and_stream(tmp_path):
    from openai import AsyncOpenAI

    app, engines = app_factory(tmp_path)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                sdk = AsyncOpenAI(
                    api_key=TOKEN,
                    base_url="http://testserver/v1",
                    http_client=http,
                    default_headers={"X-Hermes-Bridge-Client": "sdk-owner"},
                )
                assert {m.id for m in (await sdk.models.list()).data} == set(MODELS)
                assert not engines
                args = {
                    "model": BODY["model"],
                    "messages": BODY["messages"],
                    "extra_body": {"hermes_session_id": "sdk-session"},
                }
                assert (await sdk.chat.completions.create(**args)).choices[
                    0
                ].message.content == "hello"
                chunks = [
                    c
                    async for c in await sdk.chat.completions.create(
                        **args, stream=True
                    )
                ]
                assert (
                    "".join(c.choices[0].delta.content or "" for c in chunks) == "hello"
                )
                assert chunks[-1].choices[0].finish_reason == "stop"

    asyncio.run(run())


def test_real_socket_openai_stream_before_completion(tmp_path):
    import socket
    import uvicorn
    from openai import OpenAI

    release = threading.Event()

    def behavior(engine, kw):
        kw["_on_text"]("early")
        assert release.wait(3)
        return completion("early final")

    app, engines = app_factory(tmp_path, behavior)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, log_level="error", ws="none")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with httpx.Client(trust_env=False, timeout=3) as transport:
            sdk = OpenAI(
                api_key=TOKEN,
                base_url=f"http://127.0.0.1:{port}/v1",
                http_client=transport,
                default_headers={"X-Hermes-Bridge-Client": "socket-owner"},
            )
            with sdk.chat.completions.create(
                model=BODY["model"],
                messages=BODY["messages"],
                extra_body={"hermes_session_id": "socket-session"},
                stream=True,
            ) as stream:
                assert next(stream).choices[0].delta.role == "assistant"
                assert next(stream).choices[0].delta.content == "early"
                assert not release.is_set()
                release.set()
                rest = list(stream)
                assert (
                    "".join(c.choices[0].delta.content or "" for c in rest) == " final"
                )
                assert rest[-1].choices[0].finish_reason == "stop"
    finally:
        release.set()
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()
    assert engines[0].closed.is_set()


def test_cli_port_zero_readiness_health_no_inference(tmp_path):
    token_file, ready = tmp_path / "token", tmp_path / "ready.json"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claude_native_bridge.api_server",
            "--home",
            str(tmp_path),
            "--token-file",
            str(token_file),
            "--ready-file",
            str(ready),
            "--port",
            "0",
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.03)
        receipt = json.loads(ready.read_text())
        assert receipt["host"] == "127.0.0.1" and receipt["port"] > 0
        assert receipt["pid"] == process.pid
        assert TOKEN not in ready.read_text()
        with httpx.Client(trust_env=False, timeout=2) as http:
            assert http.get(receipt["health_url"]).status_code == 401
            assert http.get(receipt["health_url"], headers=HEADERS).json() == {
                "service": "claude-native-bridge",
                "status": "ok",
            }
            assert (
                http.get(receipt["base_url"] + "/models", headers=HEADERS).status_code
                == 200
            )
    finally:
        process.terminate()
        try:
            output, error = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert not ready.exists()
    assert TOKEN.encode() not in output + error


@pytest.mark.parametrize("stream", [False, True])
def test_disconnect_closes_only_active_owner(tmp_path, stream):
    entered = threading.Event()

    def behavior(engine, kw):
        if kw["messages"][0]["content"] == "idle":
            return completion()
        if "_on_text" in kw:
            kw["_on_text"]("early")
        entered.set()
        assert engine.closed.wait(3)
        raise RuntimeError("cancelled")

    app, engines = app_factory(tmp_path, behavior)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                result = await client.post(
                    "/v1/chat/completions",
                    headers=dict(HEADERS, **{"X-Hermes-Bridge-Client": "idle"}),
                    json=dict(BODY, messages=[{"role": "user", "content": "idle"}]),
                )
                assert result.status_code == 200
            task, incoming, messages, seen = await asgi_request(
                app, dict(BODY, stream=stream)
            )
            assert await asyncio.to_thread(entered.wait, 2)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 3)
            assert engines[1].closed.is_set()
            assert not engines[0].closed.is_set()
        assert engines[0].closed.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("fail_send", [True, "start"])
def test_asgi24_send_disconnect_cleanup(tmp_path, fail_send):
    def behavior(engine, kw):
        kw["_on_text"]("early")
        assert engine.closed.wait(3)
        raise RuntimeError("cancelled")

    app, engines = app_factory(tmp_path, behavior)

    async def run():
        from starlette.requests import ClientDisconnect

        async with app.router.lifespan_context(app):
            task, _, _, _ = await asgi_request(
                app, dict(BODY, stream=True), fail_send=fail_send
            )
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(task, 3)
            assert engines[0].closed.is_set()
            assert not app.state.owners.items["owner-a"].busy

    asyncio.run(run())


def test_shutdown_closes_active_engine(tmp_path):
    entered = threading.Event()

    def behavior(engine, kw):
        entered.set()
        assert engine.closed.wait(3)
        raise RuntimeError("cancelled")

    app, engines = app_factory(tmp_path, behavior)

    async def run():
        async with app.router.lifespan_context(app):
            task, _, _, _ = await asgi_request(app, dict(BODY, stream=True))
            assert await asyncio.to_thread(entered.wait, 2)
        assert engines[0].closed.is_set()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_owner_capacity_idle_pruning_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_native_bridge.api.MAX_OWNERS", 1)
    app, engines = app_factory(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code
            == 200
        )
        other = dict(HEADERS, **{"X-Hermes-Bridge-Client": "other"})
        assert (
            client.post("/v1/chat/completions", headers=other, json=BODY).status_code
            == 429
        )
        app.state.owners.items["owner-a"].touched -= 1000
        assert (
            client.post("/v1/chat/completions", headers=other, json=BODY).status_code
            == 200
        )
        assert engines[0].closed.is_set()
    monkeypatch.setattr("claude_native_bridge.api.REQUEST_TIMEOUT_SECONDS", 0.03)

    def blocked(engine, kw):
        assert engine.closed.wait(2)
        raise RuntimeError("cancelled")

    app, engines = app_factory(tmp_path, blocked)
    with TestClient(app) as client:
        assert (
            client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code
            == 502
        )
        assert engines[0].closed.is_set()
