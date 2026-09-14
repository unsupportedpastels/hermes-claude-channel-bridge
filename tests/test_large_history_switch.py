"""Offline acceptance for bounded model switches with very large canonical history.

This exercises a fake native transport only. It does not claim live 700k-token
processing or native role parity.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings

# Deterministic GPT-side fixture metadata, precomputed with tiktoken 0.11.0 and
# o200k_base. tiktoken is intentionally not a runtime or test dependency.
PAYLOAD_CHAR_COUNT = 3_400_000
PAYLOAD_O200K_TOKEN_COUNT = 1_295_104
PAYLOAD_SHA256 = "4cfab452b87e85960807accf87547140800abc51b3814cf7fd6adb76e9dbef50"
FRAME_MAX_CHARS = 80_000
SESSION_ID = "large-history-acceptance"
SWITCH_RESPONSE = "switch accepted"


class OfflineNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"offline-native-{len(self.instances)}"
        self.instances.append(self)

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": SWITCH_RESPONSE,
        }

    def close(self):
        self.closed = True


def _large_payload():
    return "".join(
        f"{index:08x}|history-payload-{index:08d}\n" for index in range(100_000)
    )


def _request(messages, model):
    return {
        "model": model,
        "messages": messages,
        "extra_body": {"hermes_session_id": SESSION_ID},
    }


def _omitted_messages(native, frame):
    notice_index = next(
        index
        for index, message in enumerate(frame["messages"])
        if isinstance(message.get("content"), str)
        and message["content"].startswith("[Earlier conversation omitted:")
    )
    match = re.fullmatch(
        r"\[Earlier conversation omitted: ([0-9]+) chars\. "
        r"Ask read_result handle '([^']+)' for older windows if needed\.\]",
        frame["messages"][notice_index]["content"],
    )
    assert match is not None
    omitted_chars, handle = match.groups()
    serialized = (native.runtime / "spool" / f"{handle}.txt").read_text()
    assert len(serialized) == int(omitted_chars)
    return notice_index, json.loads(serialized)


def test_large_history_model_switch_is_bounded_recoverable_and_continuous(tmp_path):
    payload = _large_payload()
    assert len(payload) == PAYLOAD_CHAR_COUNT
    assert hashlib.sha256(payload.encode()).hexdigest() == PAYLOAD_SHA256
    assert PAYLOAD_O200K_TOKEN_COUNT >= 700_000

    tool_call = {
        "role": "assistant",
        "content": "I will inspect both independent records.",
        "tool_calls": [
            {
                "id": "call_customer",
                "type": "function",
                "function": {
                    "name": "lookup_record",
                    "arguments": '{"record_id":"customer-42"}',
                },
            },
            {
                "id": "call_order",
                "type": "function",
                "function": {
                    "name": "lookup_record",
                    "arguments": '{"record_id":"order-9000"}',
                },
            },
        ],
    }
    tool_results = [
        {
            "role": "tool",
            "tool_call_id": "call_customer",
            "content": '{"record_id":"customer-42","notes":"' + "C" * 31_900 + '"}',
        },
        {
            "role": "tool",
            "tool_call_id": "call_order",
            "content": '{"record_id":"order-9000","notes":"' + "O" * 31_900 + '"}',
        },
    ]
    canonical = [
        {
            "role": "system",
            "content": "SYSTEM_MARKER: preserve canonical policy and Hermes identity.",
        },
        {"role": "user", "content": "OLD_HISTORY_MARKER\n" + payload},
        {"role": "user", "content": "Inspect the customer and order records."},
        tool_call,
        *tool_results,
        {
            "role": "assistant",
            "content": "Both records were inspected. " + "S" * 3_900,
        },
        {
            "role": "user",
            "content": "NEWEST_MARKER: summarize only the current status. "
            + "N" * 11_900,
        },
    ]
    original = copy.deepcopy(canonical)
    OfflineNative.instances = []
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True,
            bootstrap_max_chars=FRAME_MAX_CHARS,
            page_threshold=10_000_000,
        ),
        native_factory=OfflineNative,
    )

    try:
        client.create(
            **_request([{"role": "user", "content": "warm up"}], "claude-sonnet-5")
        )
        client.create(**_request(canonical, "claude-opus-4-8"))

        assert canonical == original
        switched = OfflineNative.instances[1]
        frame_text = switched.frames[0]
        frame = json.loads(frame_text)
        assert frame["operation"] == "bootstrap"
        assert len(frame_text) <= FRAME_MAX_CHARS
        assert frame["messages"][-1]["role"] == "user"
        assert frame["messages"][-1]["content"].startswith("NEWEST_MARKER:")

        identity = frame["messages"][0]
        assert identity["role"] == "system"
        assert f"Canonical Hermes session ID: {SESSION_ID}" in identity["content"]
        assert canonical[0] in frame["messages"]

        notice_index, omitted = _omitted_messages(switched, frame)
        before_notice = frame["messages"][1:notice_index]
        after_notice = frame["messages"][notice_index + 1 :]
        assert before_notice + omitted + after_notice == canonical

        omitted_ids = {
            message.get("tool_call_id")
            for message in omitted
            if message.get("role") == "tool"
        }
        retained_ids = {
            message.get("tool_call_id")
            for message in before_notice + after_notice
            if message.get("role") == "tool"
        }
        call_ids = {call["id"] for call in tool_call["tool_calls"]}
        assert call_ids <= omitted_ids or call_ids <= retained_ids
        tool_call_side = (
            omitted if tool_call in omitted else before_notice + after_notice
        )
        assert tool_call in tool_call_side
        assert call_ids <= {
            message.get("tool_call_id")
            for message in tool_call_side
            if message.get("role") == "tool"
        }

        next_user = {"role": "user", "content": "NEXT_DELTA_MARKER"}
        continued_canonical = canonical + [
            {"role": "assistant", "content": SWITCH_RESPONSE},
            next_user,
        ]
        client.create(**_request(continued_canonical, "claude-opus-4-8"))

        assert len(OfflineNative.instances) == 2
        continued_text = switched.frames[1]
        assert len(continued_text) <= FRAME_MAX_CHARS
        assert json.loads(continued_text) == {
            "operation": "continue",
            "messages": [next_user],
        }
    finally:
        client.close()
