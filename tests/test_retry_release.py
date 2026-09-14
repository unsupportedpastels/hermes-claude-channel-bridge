"""Offline full HTTP/provider regression for uncertain stream replay."""

import socket
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from openai import InternalServerError

from claude_native_bridge.api import create_app
from claude_native_bridge.api_provider import RETRY_LINEAGE_HEADER, make_profile

TOKEN = "test-only-retry-release-credential"
MODEL = "claude-sonnet-5"
SESSION = "stable-hermes-session"


def completion(text):
    return NS(
        id="chatcmpl-retry-release",
        created=123,
        choices=[
            NS(
                message=NS(role="assistant", content=text, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


class Engine:
    def __init__(self, block):
        self.block = block
        self.closed = threading.Event()
        self.calls = 0
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        if self.block:
            kwargs["_on_text"]("early")
            assert self.closed.wait(3)
            raise RuntimeError("cancelled after uncertain submission")
        return completion("legitimate next request")

    def close(self):
        self.closed.set()


def test_recreated_provider_client_cannot_replay_cancelled_identical_request(tmp_path):
    engines = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = Engine(block=not engines)
        engines.append(engine)
        return engine

    app = create_app(TOKEN, tmp_path, factory)
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
    first = second = independent = None
    try:
        deadline = time.monotonic() + 3
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)

        (tmp_path / "config.yaml").write_text(
            f"claude_native_bridge_api:\n  port: {port}\n"
        )
        profile = make_profile(tmp_path)
        base_url = f"http://127.0.0.1:{port}/v1"
        parent_headers = dict(profile.default_headers)
        request = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "same uncertain request"}],
            "stream": True,
            "extra_body": {"hermes_session_id": SESSION},
        }
        with patch("claude_native_bridge.api_service.ensure_server"):
            first = profile.create_client(
                api_key=TOKEN,
                base_url=base_url,
                max_retries=0,
                default_headers=parent_headers,
                http_client=httpx.Client(trust_env=False, timeout=3),
            )
            first_owner = first.default_headers["X-Hermes-Bridge-Client"]
            first_lineage = first.default_headers[RETRY_LINEAGE_HEADER]
            stream = first.chat.completions.create(**request)
            assert next(stream).choices[0].delta.role == "assistant"
            assert next(stream).choices[0].delta.content == "early"
            first.close()
            first = None

            second = profile.create_client(
                api_key=TOKEN,
                base_url=base_url,
                max_retries=0,
                default_headers=parent_headers,
                http_client=httpx.Client(trust_env=False, timeout=3),
            )
            assert second.default_headers["X-Hermes-Bridge-Client"] != first_owner
            assert second.default_headers[RETRY_LINEAGE_HEADER] == first_lineage
            with pytest.raises(InternalServerError) as refused:
                list(second.chat.completions.create(**request))
            assert refused.value.status_code == 502
            assert len(engines) == 1
            assert engines[0].calls == 1

            independent = profile.create_client(
                api_key=TOKEN,
                base_url=base_url,
                max_retries=0,
                default_headers=dict(profile.default_headers),
                http_client=httpx.Client(trust_env=False, timeout=3),
            )
            assert independent.default_headers[RETRY_LINEAGE_HEADER] != first_lineage
            allowed = dict(request, stream=False)
            result = independent.chat.completions.create(**allowed)
            assert result.choices[0].message.content == "legitimate next request"
            assert len(engines) == 2

            legitimate = dict(
                request,
                messages=[{"role": "user", "content": "legitimate next request"}],
                stream=False,
            )
            result = second.chat.completions.create(**legitimate)
            assert result.choices[0].message.content == "legitimate next request"
            assert len(engines) == 3
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        if independent is not None:
            independent.close()
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()
