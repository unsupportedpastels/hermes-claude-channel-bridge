"""Offline binding-slot eviction regressions; no native process or model calls."""

from types import SimpleNamespace

import pytest

from claude_native_bridge.client import Binding, NativeBridgeClient
from claude_native_bridge.protocol import HistoryTracker
from claude_native_bridge.settings import Settings


class FakeNative:
    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        return {
            "sequence": 1,
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


class RecordingHistory(HistoryTracker):
    def __init__(self):
        super().__init__()
        self.reset_calls = 0

    def reset(self):
        super().reset()
        if hasattr(self, "reset_calls"):
            self.reset_calls += 1


@pytest.mark.parametrize(
    "native",
    [SimpleNamespace(closed=True), None],
    ids=["native-closed", "native-none"],
)
def test_expired_binding_releases_capacity_before_next_session_admission(
    tmp_path, native
):
    bridge = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True, max_sessions=1),
        native_factory=FakeNative,
    )
    history = RecordingHistory()
    bridge._bindings["expired"] = Binding(history=history, native=native)

    try:
        response = bridge.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "new session"}],
            extra_body={"hermes_session_id": "next"},
        )
    finally:
        bridge.close()

    assert response.choices[0].message.content == "done"
    assert "expired" not in bridge._bindings
    assert "next" in bridge._bindings
    assert history.reset_calls == 1
