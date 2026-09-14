"""Offline bridge-driven native session rotation; no native process or model calls."""

import json
from pathlib import Path
from typing import ClassVar

import pytest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.native import native_child_environment, native_hook_settings
from claude_native_bridge.settings import (
    NativeBridgeError,
    Settings,
    rotation_threshold,
)
from claude_native_bridge.usage import capture_status, context_occupancy


class ContextNative:
    """Fake native reporting whatever context occupancy the test dictates."""

    instances: ClassVar[list] = []
    context: ClassVar[dict | None] = None
    compaction: ClassVar[dict | None] = None

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"run-{len(self.instances)}"
        self.last_usage = None
        self.last_context = None
        self.instances.append(self)

    @property
    def last_compaction(self):
        return None if self.compaction is None else dict(self.compaction)

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        self.last_context = None if self.context is None else dict(self.context)
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_native():
    ContextNative.instances = []
    ContextNative.context = None
    ContextNative.compaction = None


def bridge(home, **overrides):
    return NativeBridgeClient(
        hermes_home=home,
        settings=Settings(development_channels_accepted=True, **overrides),
        native_factory=ContextNative,
    )


def request(messages):
    return {
        "model": "claude-sonnet-5",
        "messages": messages,
        "extra_body": {"hermes_session_id": "rotation"},
    }


def history(turns, filler=""):
    messages = []
    for index in range(turns):
        if index:
            messages.append({"role": "assistant", "content": "done"})
        messages.append({"role": "user", "content": f"turn-{index}:" + filler})
    return messages


def operation(native, index):
    return json.loads(native.frames[index])["operation"]


def test_threshold_follows_percentage_headroom_and_optional_cap():
    settings = Settings(development_channels_accepted=True)
    assert rotation_threshold(settings, 1_000_000) == 800_000
    assert rotation_threshold(settings, 200_000) == 160_000
    capped = Settings(development_channels_accepted=True, rotation_max_tokens=200_000)
    assert rotation_threshold(capped, 1_000_000) == 200_000
    # Bootstrap characters are not a proven token bound and cannot silently
    # override the configured headroom.
    assert rotation_threshold(settings, 100_000) == 60_000
    with pytest.raises(NativeBridgeError):
        rotation_threshold(settings, 0)


def test_rotation_settings_defaults_and_validation():
    defaults = Settings.from_mapping({})
    assert defaults.native_auto_compact is False
    assert defaults.rotation_percentage == 80
    assert defaults.rotation_headroom_tokens == 40_000
    assert defaults.rotation_max_tokens is None
    assert defaults.rotation_fallback_chars == 600_000
    for config in [
        {"native_auto_compact": "no"},
        {"rotation_percentage": 0},
        {"rotation_percentage": 101},
        {"rotation_percentage": "80"},
        {"rotation_headroom_tokens": -1},
        {"rotation_headroom_tokens": 1.5},
        {"rotation_max_tokens": 0},
        {"rotation_max_tokens": True},
        {"rotation_fallback_chars": 100_000},
        {"rotation_fallback_chars": "600000"},
    ]:
        with pytest.raises(NativeBridgeError):
            Settings.from_mapping(config)
    assert Settings.from_mapping({"rotation_max_tokens": 1}).rotation_max_tokens == 1


def test_launch_settings_disable_native_auto_compaction_but_keep_sentinel_hooks():
    launch = native_hook_settings("observe", "status")
    assert launch["autoCompactEnabled"] is False
    assert {"PreCompact", "PostCompact"} <= set(launch["hooks"])
    enabled = native_hook_settings("observe", "status", auto_compact=True)
    assert enabled["autoCompactEnabled"] is True
    env = native_child_environment(Settings(), {"PATH": "/bin", "HOME": "/home/x"})
    assert env["DISABLE_AUTO_COMPACT"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["PATH"] == "/bin"
    opted_in = native_child_environment(
        Settings(native_auto_compact=True), {"PATH": "/bin"}
    )
    assert "DISABLE_AUTO_COMPACT" not in opted_in
    assert opted_in["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_status_capture_and_occupancy_use_native_counters_only(tmp_path):
    (tmp_path / "launch.json").write_text(json.dumps({"session_id": "s"}))
    payload = {
        "session_id": "s",
        "model": {"id": "claude-sonnet-5"},
        "prompt_cache": {"requests": 1},
        "context_window": {
            "context_window_size": 200_000,
            "used_percentage": 12.5,
            "current_usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 900,
            },
        },
    }
    assert capture_status(tmp_path, payload)
    saved = json.loads((tmp_path / "native-usage.json").read_text())
    assert saved["context_window"]["context_window_size"] == 200_000
    assert "used_percentage" not in saved["context_window"]
    assert context_occupancy(saved, "s", "claude-sonnet-5") == {
        "tokens": 1030,
        "window": 200_000,
    }
    del saved["context_window"]["context_window_size"]
    assert context_occupancy(saved, "s", "claude-sonnet-5") == {
        "tokens": 1030,
        "window": None,
    }
    assert context_occupancy(saved, "other", "claude-sonnet-5") is None
    assert context_occupancy(saved, "s", "claude-opus-4-8") is None
    saved["context_window"]["current_usage"]["input_tokens"] = "10"
    assert context_occupancy(saved, "s", "claude-sonnet-5") is None


def test_below_threshold_continues_on_the_same_native_session(tmp_path):
    ContextNative.context = {"tokens": 100_000, "window": 1_000_000}
    client = bridge(tmp_path)
    try:
        for turns in (1, 2, 3):
            completion = client.create(**request(history(turns)))
            assert not hasattr(completion, "native_bridge_rotation")
        assert len(ContextNative.instances) == 1
        native = ContextNative.instances[0]
        assert [operation(native, i) for i in range(3)] == [
            "bootstrap",
            "continue",
            "continue",
        ]
    finally:
        client.close()


def test_over_threshold_rotates_between_requests_with_bounded_bootstrap(tmp_path):
    ContextNative.context = {"tokens": 170_000, "window": 200_000}
    filler = "x" * 400
    client = bridge(tmp_path, bootstrap_max_chars=2_000)
    try:
        first = client.create(**request(history(1, filler)))
        assert not hasattr(first, "native_bridge_rotation")

        # The rotation decision uses the counters recorded by the retired
        # session; the rebuilt session then reports its own smaller context.
        ContextNative.context = {"tokens": 30_000, "window": 200_000}
        rotated = client.create(**request(history(6, filler)))
        assert rotated.native_bridge_rotation == {
            "rotated": True,
            "reason": "context_tokens",
            "observed_tokens": 170_000,
            "threshold_tokens": 160_000,
            "window_tokens": 200_000,
            "window_source": "native_status_line",
            "native_exchanges": 1,
            "telemetry_exchange": 1,
        }
        old, new = ContextNative.instances
        assert old.closed and not new.closed
        assert len(old.frames) == 1
        assert operation(new, 0) == "bootstrap"
        assert len(new.frames[0]) <= 2_000
        assert "turn-5" in new.frames[0]
        assert "[Earlier conversation omitted:" in new.frames[0]

        # The rebuilt session continues normally while it has headroom.
        following = client.create(**request(history(7, filler)))
        assert not hasattr(following, "native_bridge_rotation")
        assert len(ContextNative.instances) == 2
        assert operation(new, 1) == "continue"
    finally:
        client.close()


def test_assumed_window_when_status_line_omits_size(tmp_path):
    ContextNative.context = {"tokens": 165_000, "window": None}
    client = bridge(tmp_path)
    try:
        client.create(**request(history(1)))
        rotated = client.create(**request(history(2)))
        assert rotated.native_bridge_rotation["window_source"] == "assumed"
        assert rotated.native_bridge_rotation["window_tokens"] == 200_000
        assert rotated.native_bridge_rotation["threshold_tokens"] == 160_000
        assert len(ContextNative.instances) == 2
    finally:
        client.close()


def test_rotation_serves_the_request_from_exactly_one_fresh_session(tmp_path):
    ContextNative.context = {"tokens": 999_000, "window": 1_000_000}
    client = bridge(tmp_path)
    try:
        client.create(**request(history(1)))
        client.create(**request(history(2)))
        assert len(ContextNative.instances) == 2
        fresh = ContextNative.instances[1]
        assert len(fresh.frames) == 1
        assert operation(fresh, 0) == "bootstrap"
    finally:
        client.close()


def test_uncorrelated_usage_falls_back_to_sent_characters(tmp_path):
    ContextNative.context = None
    filler = "y" * 1_000
    client = bridge(tmp_path, bootstrap_max_chars=3_000, rotation_fallback_chars=6_000)
    try:
        rotations = []
        for turns in range(1, 9):
            completion = client.create(**request(history(turns, filler)))
            rotation = getattr(completion, "native_bridge_rotation", None)
            if rotation is not None:
                rotations.append(rotation)
        assert rotations
        assert rotations[0]["reason"] == "uncorrelated_usage_chars"
        assert (
            rotations[0]["observed_chars"]
            + rotations[0]["incoming_estimate"]["chars"]
            > 6_000
        )
        assert rotations[0]["threshold_chars"] == 6_000
        assert rotations[0]["incoming_estimate"]["native_tokens"] is None
        assert len(ContextNative.instances) == 1 + len(rotations)
        for native in ContextNative.instances:
            assert operation(native, 0) == "bootstrap"
            assert len(native.frames[0]) <= 3_000
    finally:
        client.close()


def test_unexpected_native_auto_compaction_is_flagged(tmp_path):
    compaction = {
        "status": "completed",
        "trigger": "auto",
        "request_id": "native-request-1",
        "active_request": True,
        "generation": 3,
        "summary_bytes": 2048,
        "error": None,
    }
    ContextNative.compaction = compaction
    client = bridge(tmp_path)
    try:
        completion = client.create(**request(history(1)))
        assert completion.native_bridge_compaction["trigger"] == "auto"
        assert completion.native_bridge_unexpected_compaction is True
    finally:
        client.close()

    ContextNative.instances = []
    ContextNative.compaction = dict(compaction, trigger="manual")
    client = bridge(tmp_path)
    try:
        completion = client.create(**request(history(1)))
        assert not hasattr(completion, "native_bridge_unexpected_compaction")
    finally:
        client.close()

    ContextNative.instances = []
    ContextNative.compaction = compaction
    client = bridge(tmp_path, native_auto_compact=True)
    try:
        completion = client.create(**request(history(1)))
        assert not hasattr(completion, "native_bridge_unexpected_compaction")
    finally:
        client.close()
