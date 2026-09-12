import json
import re
from pathlib import Path

import pytest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import NativeBridgeError, Settings


class BootstrapNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"run-{len(self.instances)}"
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
            "text": "done",
        }

    def close(self):
        self.closed = True


def request(messages, model="claude-sonnet-5"):
    return {
        "model": model,
        "messages": messages,
        "extra_body": {"hermes_session_id": "bootstrap"},
    }


def test_model_switch_bootstrap_is_bounded_and_spools_omitted_prefix(tmp_path):
    BootstrapNative.instances = []
    maximum = 100_000
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True,
            bootstrap_max_chars=maximum,
        ),
        native_factory=BootstrapNative,
    )
    initial = [{"role": "user", "content": "first"}]
    huge = [
        {"role": "user", "content": f"window-{index}:" + chr(65 + index) * 69_990}
        for index in range(10)
    ]

    try:
        client.create(**request(initial))
        client.create(**request(huge, model="claude-opus-4-8"))

        switched = BootstrapNative.instances[1]
        frame_text = switched.frames[0]
        frame = json.loads(frame_text)
        assert len(frame_text) <= maximum
        assert frame["operation"] == "bootstrap"
        notice = frame["messages"][0]["content"]
        match = re.fullmatch(
            r"\[Earlier conversation omitted: ([0-9]+) chars\. "
            r"Ask read_result handle '([^']+)' for older windows if needed\.\]",
            notice,
        )
        assert match is not None
        omitted_chars, handle = match.groups()
        spool = switched.runtime / "spool" / f"{handle}.txt"
        omitted = spool.read_text()
        assert len(omitted) == int(omitted_chars)
        assert "window-0" in omitted
        assert "window-9" in frame_text
        assert "window-0" not in frame_text
    finally:
        client.close()


def test_effort_switch_keeps_tail_of_one_oversized_message(tmp_path):
    BootstrapNative.instances = []
    maximum = 100_000
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True,
            bootstrap_max_chars=maximum,
        ),
        native_factory=BootstrapNative,
    )
    huge = [{"role": "user", "content": "x" * 700_000 + "newest-marker"}]

    try:
        client.create(**request([{"role": "user", "content": "first"}]))
        changed = request(huge)
        changed["reasoning_effort"] = "high"
        client.create(**changed)

        frame_text = BootstrapNative.instances[1].frames[0]
        frame = json.loads(frame_text)
        assert len(frame_text) <= maximum
        assert frame["messages"][0]["content"].startswith(
            "[Earlier conversation omitted:"
        )
        assert frame["messages"][-1]["content"].endswith("newest-marker")
        assert len(frame["messages"][-1]["content"]) > len("newest-marker")
    finally:
        client.close()


def test_tail_history_becomes_committed_baseline_after_switch(tmp_path):
    BootstrapNative.instances = []
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True,
            bootstrap_max_chars=1_000,
        ),
        native_factory=BootstrapNative,
    )
    original = [{"role": "user", "content": "old"}]
    switched_history = [
        {"role": "user", "content": f"turn-{index}:" + "x" * 300}
        for index in range(8)
    ]

    try:
        client.create(**request(original))
        client.create(**request(switched_history, model="claude-opus-4-8"))
        following = switched_history + [
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"},
        ]
        client.create(**request(following, model="claude-opus-4-8"))

        assert len(BootstrapNative.instances) == 2
        continued = json.loads(BootstrapNative.instances[1].frames[1])
        assert continued == {
            "operation": "continue",
            "messages": [{"role": "user", "content": "next"}],
        }
    finally:
        client.close()


def test_bootstrap_max_chars_defaults_and_rejects_invalid_values():
    assert Settings.from_mapping({}).bootstrap_max_chars == 100_000
    for value in (0, -1, True, 1.5, "100000"):
        with pytest.raises(NativeBridgeError, match="bootstrap_max_chars"):
            Settings.from_mapping({"bootstrap_max_chars": value})


def test_divergence_rebuild_is_also_bounded(tmp_path):
    """Source-history divergence (e.g. Hermes compression) re-bootstraps;
    that path must respect bootstrap_max_chars too."""
    BootstrapNative.instances = []
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(
            development_channels_accepted=True,
            bootstrap_max_chars=1_000,
        ),
        native_factory=BootstrapNative,
    )
    try:
        client.create(**request([{"role": "user", "content": "hi"}]))
        big = [
            {"role": "user", "content": f"turn-{i}:" + "x" * 300}
            for i in range(8)
        ]
        client.create(**request(big, model="claude-opus-4-8"))
        divergent = [{"role": "user", "content": "rewritten:" + "y" * 900}]
        client.create(**request(divergent, model="claude-opus-4-8"))

        raw = BootstrapNative.instances[-1].frames[0]
        assert len(raw) <= 1_000
        final = json.loads(raw)
        assert any(
            "omitted" in str(m.get("content", "")) for m in final["messages"]
        )
    finally:
        client.close()
