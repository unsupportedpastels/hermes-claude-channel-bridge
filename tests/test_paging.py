import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native import NativeSession
from claude_native_bridge.settings import NativeBridgeError, Settings


class PagingNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / "run"
        self.instances.append(self)

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        if len(self.frames) == 1:
            return {
                "sequence": 1,
                "request_id": request_id,
                "kind": "tool_calls",
                "tool_calls": [{"name": "fixture_tool", "arguments": {}}],
            }
        return {
            "sequence": 2,
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def test_oversized_tool_result_becomes_small_handle_envelope(tmp_path):
    PagingNative.instances = []
    settings = Settings(
        development_channels_accepted=True,
        page_threshold=20_000,
    )
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=settings,
        native_factory=PagingNative,
    )
    tools = [
        {
            "type": "function",
            "function": {"name": "fixture_tool", "parameters": {}},
        }
    ]
    messages = [{"role": "user", "content": "fetch it"}]

    try:
        first = client.create(
            model="claude-sonnet-5",
            messages=messages,
            tools=tools,
            extra_body={"hermes_session_id": "paging"},
        )
        call = first.choices[0].message.tool_calls[0]
        huge = "start:" + "x" * 68_990 + ":end"
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call.id, "content": huge},
        ]
        client.create(
            model="claude-sonnet-5",
            messages=messages,
            tools=tools,
            extra_body={"hermes_session_id": "paging"},
        )

        native = PagingNative.instances[0]
        frame_text = native.frames[1]
        frame = json.loads(frame_text)
        result = frame["messages"][-1]
        envelope = result["content"]
        assert result["tool_call_id"] == call.id
        assert huge not in frame_text
        assert len(frame_text) < 1_000
        assert envelope.startswith(f"Result too large ({len(huge):,} chars). Handle: ")
        assert envelope.endswith(
            '\nCall read_result(handle="'
            + envelope.split("Handle: ", 1)[1].split(".", 1)[0]
            + '", offset=0, length=15000) to read it in pages.'
        )
        handle = envelope.split("Handle: ", 1)[1].split(".", 1)[0]
        assert (native.runtime / "spool" / f"{handle}.txt").read_text() == huge
    finally:
        client.close()


def test_page_threshold_defaults_and_rejects_invalid_values():
    assert Settings.from_mapping({}).page_threshold == 20_000
    for value in (0, -1, True, 1.5, "20000"):
        with pytest.raises(NativeBridgeError, match="page_threshold"):
            Settings.from_mapping({"page_threshold": value})


def test_native_close_expires_spool_with_default_runtime_cleanup(tmp_path):
    runtime = tmp_path / "run"
    spool = runtime / "spool"
    spool.mkdir(parents=True)
    (spool / "rfixture.txt").write_text("result")
    native = NativeSession(
        Settings(), tmp_path, "claude-sonnet-5", "medium", http_client=object()
    )
    native.runtime = runtime
    with patch.object(native, "_tmux"):
        native.close()

    assert not runtime.exists()
