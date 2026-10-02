"""Image delivery through the real client with a recording native double.

The native double is a mock: it proves what the bridge sends and stores, not what
Claude does with it. The real MCP reader is exercised in test_image_mcp.py and
channel/images.test.mjs.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from claude_native_bridge import images
from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import NativeRequestNotDelivered, Settings

from image_fixtures import GIF, JPEG, PNG, image_part

TOOLS = [{"type": "function", "function": {"name": "fixture_tool", "parameters": {}}}]
# Frames and spools are JSON, so the placeholder's quotes appear escaped there.
HANDLE = re.compile(r'handle=\\?"(i[0-9a-f]{32})')


class RecordingNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"run{len(self.instances)}"
        self.instances.append(self)

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(json.loads(content))
        if self.script:
            return {"sequence": len(self.frames), "request_id": request_id, **self.script.pop(0)}
        return {"sequence": len(self.frames), "request_id": request_id, "kind": "final", "text": "ok"}

    script = None

    def close(self):
        self.closed = True


@pytest.fixture
def client(tmp_path):
    RecordingNative.instances = []
    RecordingNative.script = None
    bridge = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
        native_factory=RecordingNative,
    )
    yield bridge
    bridge.close()


def ask(client, messages, tools=None, choice=None, session="s"):
    return client.create(
        model="claude-sonnet-5",
        messages=messages,
        tools=tools,
        tool_choice=choice,
        extra_body={"hermes_session_id": session},
    )


def text(value):
    return {"type": "text", "text": value}


def handles(frame):
    return HANDLE.findall(json.dumps(frame))


def stored(native):
    directory = native.runtime / "images"
    return sorted(p.read_bytes() for p in directory.iterdir()) if directory.exists() else []


def tool_round(first):
    call = first.choices[0].message.tool_calls[0]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
            ],
        },
        call.id,
    ]


def test_user_images_reach_native_as_ordered_handles_only(client):
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": [text("look"), image_part(PNG), text("and"), image_part(JPEG, "image/jpeg")]},
    ]
    original = copy.deepcopy(messages)

    ask(client, messages)

    native = RecordingNative.instances[0]
    frame = native.frames[0]
    parts = frame["messages"][-1]["content"]
    assert [p["text"] for p in parts if "image" not in p["text"].lower()] == ["look", "and"]
    assert ["look", "and"] == [parts[0]["text"], parts[2]["text"]]
    assert "mcp__hermesbridge__read_image" in parts[1]["text"]
    assert "mcp__hermesbridge__read_image" in parts[3]["text"]
    assert "base64" not in json.dumps(frame)
    assert stored(native) == sorted([PNG, JPEG])
    assert messages == original, "canonical Hermes messages must stay untouched"


def test_tool_result_image_arrives_in_the_continuation_in_order(client):
    RecordingNative.script = [
        {"kind": "tool_calls", "tool_calls": [{"name": "fixture_tool", "arguments": {}}]},
        {"kind": "final", "text": "seen"},
    ]
    messages = [{"role": "user", "content": "screenshot please"}]
    first = ask(client, messages, TOOLS)
    assistant, call_id = tool_round(first)
    messages += [
        assistant,
        {"role": "tool", "tool_call_id": call_id, "content": [text("captured"), image_part(GIF, "image/gif")]},
    ]

    ask(client, messages, TOOLS)

    native = RecordingNative.instances[0]
    assert len(RecordingNative.instances) == 1
    frame = native.frames[1]
    assert frame["operation"] == "continue"
    tool = frame["messages"][-1]
    assert tool["tool_call_id"] == call_id
    assert tool["content"][0] == text("captured")
    assert handles(tool) and "base64" not in json.dumps(frame)
    assert stored(native) == [GIF]


def test_retained_images_keep_one_native_session_and_stable_handles(client):
    messages = [{"role": "user", "content": [text("a"), image_part(PNG)]}]
    ask(client, messages)
    first = handles(RecordingNative.instances[0].frames[0])
    messages += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}]
    ask(client, messages)
    native = RecordingNative.instances[0]
    assert len(RecordingNative.instances) == 1
    assert native.frames[1]["operation"] == "continue"
    assert handles(native.frames[1]) == []
    assert stored(native) == [PNG] and first


def test_history_that_drops_images_rebuilds_without_resurrecting_them(client):
    old = [{"role": "user", "content": [text("a"), image_part(PNG)]}]
    ask(client, old)
    first_runtime = RecordingNative.instances[0].runtime
    compressed = [
        {"role": "user", "content": [text("a"), text("[image stripped by Hermes]")]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]

    ask(client, compressed)

    first, second = RecordingNative.instances
    assert first.closed and len(RecordingNative.instances) == 2
    assert second.frames[0]["operation"] == "bootstrap"
    assert "image" not in json.dumps(second.frames[0]).replace("[image stripped", "")
    assert not (first_runtime / "images").exists() or stored(first) == []
    assert stored(second) == []


def test_rebuild_re_externalizes_only_images_in_the_current_request(client):
    messages = [{"role": "user", "content": [text("a"), image_part(PNG)]}]
    ask(client, messages, TOOLS)
    ask(client, messages, None)  # tool schema change forces a rebuild

    first, second = RecordingNative.instances
    assert first.closed
    assert second.frames[0]["operation"] == "bootstrap"
    assert handles(second.frames[0]) == handles(first.frames[0])
    assert stored(second) == [PNG]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}},
        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!"}},
        {"type": "image_url", "image_url": {"url": "data:image/bmp;base64,Qk0="}},
    ],
)
def test_bad_images_fail_before_native_and_say_not_seen(client, part):
    with pytest.raises(NativeRequestNotDelivered, match="not seen"):
        ask(client, [{"role": "user", "content": [text("x"), part]}])
    assert all(not native.frames for native in RecordingNative.instances)


def test_oversize_image_fails_before_native(client, monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 16)
    with pytest.raises(NativeRequestNotDelivered, match="not seen"):
        ask(client, [{"role": "user", "content": [image_part(PNG + b"x" * 100)]}])
    assert all(not native.frames for native in RecordingNative.instances)


def test_assistant_images_are_rejected(client):
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": [image_part()]},
        {"role": "user", "content": "y"},
    ]
    with pytest.raises(NativeRequestNotDelivered, match="not seen"):
        ask(client, messages)


def test_text_only_requests_are_unchanged_and_create_no_store(client):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "plain"}]
    ask(client, messages)
    native = RecordingNative.instances[0]
    assert native.frames[0]["messages"][-1] == {"role": "user", "content": "plain"}
    assert not (native.runtime / "images").exists()


def test_bounded_bootstrap_keeps_omitted_images_addressable(tmp_path):
    RecordingNative.instances = []
    RecordingNative.script = None
    settings = Settings(development_channels_accepted=True, bootstrap_max_chars=4_000)
    bridge = NativeBridgeClient(
        hermes_home=tmp_path, settings=settings, native_factory=RecordingNative
    )
    try:
        messages = [{"role": "system", "content": "rules"}]
        messages += [
            {"role": "user", "content": [text("early " + "w" * 1500), image_part(PNG)]},
            {"role": "assistant", "content": "a" * 1500},
            {"role": "user", "content": "b" * 1500},
            {"role": "assistant", "content": "c" * 1500},
            {"role": "user", "content": "current"},
        ]
        ask(bridge, messages)
        native = RecordingNative.instances[0]
        frame = json.dumps(native.frames[0])
        assert "Earlier conversation omitted" in frame
        assert "base64" not in frame
        spooled = "".join(p.read_text() for p in (native.runtime / "spool").iterdir())
        assert HANDLE.search(spooled), "omitted image placeholder is reachable via read_result"
        assert stored(native) == [PNG]
    finally:
        bridge.close()
