"""Offline host-shaped integration for logical bridge ownership."""

import socket
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from openai import RateLimitError

from claude_native_bridge.api import create_app
from claude_native_bridge.api_provider import RETRY_LINEAGE_HEADER, make_profile

TOKEN = "test-only-logical-owner-credential"
MODEL = "claude-sonnet-5"
SESSION = "logical-owner-session"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {"round": {"type": "integer"}},
                "required": ["round"],
            },
        },
    }
]


def _completion(*, text=None, tool_round=None):
    tool_calls = None
    finish = "stop"
    if tool_round is not None:
        tool_calls = [
            NS(
                id=f"call-{tool_round}",
                type="function",
                function=NS(name="lookup", arguments=f'{{"round":{tool_round}}}'),
            )
        ]
        finish = "tool_calls"
    return NS(
        id="chatcmpl-logical-owner",
        created=123,
        choices=[
            NS(
                message=NS(role="assistant", content=text, tool_calls=tool_calls),
                finish_reason=finish,
            )
        ],
        usage=None,
    )


class Engine:
    def __init__(self):
        self.calls = []
        self.closed = threading.Event()
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            return _completion(tool_round=len(self.calls))
        return _completion(text="bounded summary")

    def close(self):
        self.closed.set()


def test_request_wrappers_and_primary_share_one_logical_owner(tmp_path):
    engines = []

    def factory(**kwargs):
        assert kwargs == {"hermes_home": tmp_path}
        engine = Engine()
        engines.append(engine)
        return engine

    app = create_app(TOKEN, tmp_path, factory, owner_limit=1)
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
    primary = request_client = None
    try:
        deadline = time.monotonic() + 3
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)

        (tmp_path / "config.yaml").write_text(
            f"claude_native_bridge_api:\n  port: {port}\n"
        )
        profile = make_profile(tmp_path)
        parent_headers = dict(profile.default_headers)
        base_url = f"http://127.0.0.1:{port}/v1"
        with patch("claude_native_bridge.api_service.ensure_server"):
            # This is the actual Hermes shape: shared primary first, then a
            # request-scoped SDK wrapper rebuilt from the same client kwargs.
            primary = profile.create_client(
                api_key=TOKEN,
                base_url=base_url,
                max_retries=0,
                default_headers=parent_headers,
                http_client=httpx.Client(trust_env=False, timeout=3),
            )
            request_client = profile.create_client(
                api_key=TOKEN,
                base_url=base_url,
                max_retries=0,
                default_headers=parent_headers,
                http_client=httpx.Client(trust_env=False, timeout=3),
            )
            assert primary.default_headers[RETRY_LINEAGE_HEADER] == request_client.default_headers[
                RETRY_LINEAGE_HEADER
            ]

            messages = [{"role": "user", "content": "do two rounds"}]
            for tool_round in (1, 2):
                response = request_client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOLS,
                    extra_body={"hermes_session_id": SESSION},
                )
                call = response.choices[0].message.tool_calls[0]
                assert call.function.arguments == f'{{"round":{tool_round}}}'
                messages.extend(
                    [
                        response.choices[0].message.model_dump(),
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"round {tool_round} result",
                        },
                    ]
                )

            # Binding remains part of the identity: sharing a lineage does not
            # let another session consume the occupied single slot.
            with pytest.raises(RateLimitError):
                primary.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": "other binding"}],
                    extra_body={"hermes_session_id": "other-session"},
                )
            assert len(engines) == 1

            # Closing one wrapper lease must not retire the logical owner while
            # the shared primary wrapper still references the same lineage.
            request_client.close()
            request_client = None
            assert not engines[0].closed.is_set()

            summary = primary.chat.completions.create(
                model=MODEL,
                messages=messages,
                extra_body={"hermes_session_id": SESSION},
            )
            assert summary.choices[0].message.content == "bounded summary"
            assert len(engines) == 1
            assert len(engines[0].calls) == 3

            primary.close()
            primary = None
            deadline = time.monotonic() + 3
            while app.state.owners.items and time.monotonic() < deadline:
                time.sleep(0.01)
            assert engines[0].closed.is_set()
            assert app.state.owners.items == {}
    finally:
        if request_client is not None:
            request_client.close()
        if primary is not None:
            primary.close()
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()
