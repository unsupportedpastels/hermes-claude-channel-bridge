"""Offline rotation freshness and admission lifecycle tests; no native calls."""

from pathlib import Path
from typing import ClassVar

import pytest

from claude_native_bridge.client import NativeBridgeClient, _bounded_bootstrap
from claude_native_bridge.settings import (
    NativeBridgeError,
    Settings,
    rotation_threshold,
)


class FreshnessNative:
    instances: ClassVar[list] = []
    contexts: ClassVar[list[dict | None]] = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.runtime = Path(home) / f"freshness-{len(self.instances)}"
        self.last_context = None
        self.last_usage = None
        self.instances.append(self)

    @property
    def last_compaction(self):
        return None

    def start(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        context = self.contexts.pop(0) if self.contexts else None
        self.last_context = None if context is None else dict(context)
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
    FreshnessNative.instances = []
    FreshnessNative.contexts = []


def bridge(home, **overrides):
    return NativeBridgeClient(
        hermes_home=home,
        settings=Settings(development_channels_accepted=True, **overrides),
        native_factory=FreshnessNative,
    )


def request(messages):
    return {
        "model": "claude-sonnet-5",
        "messages": messages,
        "extra_body": {"hermes_session_id": "freshness"},
    }


def history(*user_texts):
    messages = []
    for index, text in enumerate(user_texts):
        if index:
            messages.append({"role": "assistant", "content": "done"})
        messages.append({"role": "user", "content": text})
    return messages


def test_threshold_never_uses_bootstrap_characters_to_override_headroom():
    settings = Settings(
        development_channels_accepted=True,
        bootstrap_max_chars=100_000,
        rotation_percentage=80,
        rotation_headroom_tokens=400,
    )
    assert rotation_threshold(settings, 1_000) == 600
    capped = Settings(
        development_channels_accepted=True,
        bootstrap_max_chars=100_000,
        rotation_percentage=80,
        rotation_headroom_tokens=100,
        rotation_max_tokens=500,
    )
    assert rotation_threshold(capped, 1_000) == 500
    with pytest.raises(NativeBridgeError, match="headroom"):
        rotation_threshold(
            Settings(
                development_channels_accepted=True,
                rotation_headroom_tokens=1_000,
            ),
            1_000,
        )


def test_missing_exchange_telemetry_clears_old_context_instead_of_reusing_it(tmp_path):
    FreshnessNative.contexts = [
        {"tokens": 680, "window": 1_000},
        None,
        None,
    ]
    client = bridge(tmp_path, rotation_headroom_tokens=200, rotation_fallback_chars=10_000)
    try:
        client.create(**request(history("first")))
        second = client.create(**request(history("first", "a")))
        assert not hasattr(second, "native_bridge_rotation")
        assert client._bindings["freshness"].context is None
        assert client._bindings["freshness"].exchanges == 2
        third = client.create(**request(history("first", "a", "z" * 100)))
        assert not hasattr(third, "native_bridge_rotation")
        assert len(FreshnessNative.instances) == 1
        assert len(FreshnessNative.instances[0].frames) == 3
    finally:
        client.close()


def test_incoming_delta_is_admitted_with_a_bounded_non_native_estimate(tmp_path):
    FreshnessNative.contexts = [
        {"tokens": 650, "window": 1_000},
        {"tokens": 100, "window": 1_000},
        {"tokens": 120, "window": 1_000},
    ]
    client = bridge(tmp_path, rotation_headroom_tokens=200, rotation_fallback_chars=10_000)
    try:
        client.create(**request(history("first")))
        rotated = client.create(**request(history("first", "x" * 300)))
        evidence = rotated.native_bridge_rotation
        assert evidence["reason"] == "incoming_admission"
        assert evidence["observed_tokens"] == 650
        assert evidence["threshold_tokens"] == 800
        assert evidence["telemetry_exchange"] == 1
        assert evidence["incoming_estimate"]["source"] == "utf8_bytes_conservative_bound"
        assert evidence["incoming_estimate"]["native_tokens"] is None
        assert evidence["incoming_estimate"]["saturated"] is True
        assert evidence["incoming_estimate"]["bytes"] == 151
        assert len(FreshnessNative.instances) == 2

        following = client.create(**request(history("first", "x" * 300, "tiny")))
        assert not hasattr(following, "native_bridge_rotation")
        assert len(FreshnessNative.instances) == 2
    finally:
        client.close()


def test_missing_telemetry_uses_projected_character_fallback(tmp_path):
    FreshnessNative.contexts = [None, None]
    client = bridge(
        tmp_path,
        bootstrap_max_chars=1_200,
        rotation_fallback_chars=900,
        rotation_headroom_tokens=200,
    )
    try:
        first = client.create(**request(history("first")))
        assert not hasattr(first, "native_bridge_rotation")
        rotated = client.create(**request(history("first", "y" * 800)))
        evidence = rotated.native_bridge_rotation
        assert evidence["reason"] == "uncorrelated_usage_chars"
        assert evidence["incoming_estimate"]["source"] == "serialized_frame_chars"
        assert evidence["incoming_estimate"]["native_tokens"] is None
        assert len(FreshnessNative.instances) == 2
    finally:
        client.close()


def test_bounded_bootstrap_refuses_to_omit_the_current_user_task(tmp_path):
    native = FreshnessNative(Settings(), tmp_path, "claude-sonnet-5", "medium")
    native.start()
    messages = [
        {"role": "user", "content": "old:" + "o" * 500},
        {"role": "user", "content": "CURRENT-TASK:" + "c" * 500},
        {"role": "developer", "content": "mandatory-after-task"},
    ]
    # This cap can fit the metadata, mandatory instruction, and omission notices,
    # but cannot fit the current task. The bridge must reject rather than silently
    # spool the task while sending only instructions to the native model.
    with pytest.raises(NativeBridgeError, match="current user task"):
        _bounded_bootstrap(messages, None, None, native, 500)
    assert not (native.runtime / "spool").exists()
