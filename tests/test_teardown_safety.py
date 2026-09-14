import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from claude_native_bridge.client import Binding, NativeBridgeClient
from claude_native_bridge.native import NativeSession, process_start
from claude_native_bridge.protocol import HistoryTracker
from claude_native_bridge.settings import NativeBridgeError, Settings


class FaultingHTTP:
    def __init__(self, message="http close failed"):
        self.calls = 0
        self.message = message

    def close(self):
        self.calls += 1
        raise RuntimeError(self.message)


class ClosingNative:
    def __init__(self, name, *, error=None, released=True):
        self.name = name
        self.error = error
        self.calls = 0
        self.cleanup_outcome = SimpleNamespace(safe_to_release_capacity=released)

    def close(self):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.cleanup_outcome


def test_client_close_attempts_every_binding_and_http_before_surfacing_errors(tmp_path):
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
    )
    first = ClosingNative("first", error="first close failed", released=False)
    second = ClosingNative("second")
    http = FaultingHTTP()
    client._client = http
    client._bindings = {
        "one": Binding(HistoryTracker(), native=first),
        "two": Binding(HistoryTracker(), native=second),
    }

    with pytest.raises(NativeBridgeError) as caught:
        client.close()

    assert first.calls == second.calls == 1
    assert http.calls == 1
    assert "first close failed" in str(caught.value)
    assert "http close failed" in str(caught.value)
    assert client.cleanup_outcome.bindings_attempted == 2
    assert client.cleanup_outcome.uncertain_bindings == ("one",)
    assert not client.cleanup_outcome.safe_to_release_capacity
    assert not client.cleanup_resource_free
    assert not client.cleanup_confirmed
    assert set(client._bindings) == {"one", "two"}


def test_client_exposes_explicit_resource_free_capability_to_api_owner(tmp_path):
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True),
    )
    native = ClosingNative("only")
    client._bindings = {"only": Binding(HistoryTracker(), native=native)}

    outcome = client.close()

    assert outcome.safe_to_release_capacity
    assert client.cleanup_resource_free
    assert client.cleanup_confirmed


def test_closed_flag_alone_does_not_release_binding_capacity(tmp_path):
    client = NativeBridgeClient(
        hermes_home=tmp_path,
        settings=Settings(development_channels_accepted=True, max_sessions=1),
    )
    client._bindings["old"] = Binding(
        HistoryTracker(),
        native=SimpleNamespace(closed=True, runtime=tmp_path / "still-live"),
    )
    try:
        with pytest.raises(NativeBridgeError, match="session limit"):
            client.create(
                model="claude-sonnet-5",
                messages=[{"role": "user", "content": "new"}],
                extra_body={"hermes_session_id": "new"},
            )
    finally:
        # The deliberately incomplete fixture has no physical-cleanup capability.
        client._bindings.clear()
        client.close()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_native_close_terminates_and_verifies_a_harmless_process(tmp_path, monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "native-pid.json").write_text(
        json.dumps({"pid": process.pid, "start": process_start(process.pid)})
    )
    session = NativeSession(
        Settings(retain_diagnostics=False),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=SimpleNamespace(close=lambda: None),
    )
    session.runtime = runtime
    session._tmux = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1)

    from claude_native_bridge import native

    real_process_start = native.process_start

    def reaping_process_start(pid):
        process.poll()  # Reap our direct-child fixture once the signal takes effect.
        return real_process_start(pid)

    monkeypatch.setattr(native, "process_start", reaping_process_start)
    try:
        outcome = session.close()
        assert outcome.process_dead
        assert outcome.safe_to_release_capacity
        assert outcome.process_identity_verified
        assert not runtime.exists()
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_native_close_attempts_later_phases_and_retains_uncertain_diagnostics(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "native-pid.json").write_text(
        json.dumps({"pid": 4242, "start": "same-identity"})
    )
    session = NativeSession(
        Settings(retain_diagnostics=False),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=FaultingHTTP(),
    )
    session._owns_http_client = True
    session.runtime = runtime
    tmux_calls = []

    def tmux(*args, **kwargs):
        tmux_calls.append(args[0])
        raise OSError("tmux unavailable")

    session._tmux = tmux
    monkeypatch.setattr(
        "claude_native_bridge.native.process_start", lambda pid: "same-identity"
    )
    monkeypatch.setattr("claude_native_bridge.native.os.getpgid", lambda pid: pid)
    kill_calls = []

    def killpg(pid, sig):
        kill_calls.append(sig)
        if sig == signal.SIGTERM:
            raise PermissionError("term denied")

    monkeypatch.setattr("claude_native_bridge.native.os.killpg", killpg)
    monkeypatch.setattr("claude_native_bridge.native.time.sleep", lambda _: None)

    with pytest.raises(NativeBridgeError) as caught:
        session.close()

    assert tmux_calls == ["kill-server", "has-session"]
    assert kill_calls == [signal.SIGTERM, signal.SIGKILL]
    assert session.http_client.calls == 1
    assert runtime.exists()
    assert not session.cleanup_outcome.process_dead
    assert not session.cleanup_outcome.safe_to_release_capacity
    assert session.cleanup_outcome.diagnostics_retained
    assert "tmux unavailable" in str(caught.value)
    assert "term denied" in str(caught.value)
    assert "http close failed" in str(caught.value)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_posix_group_identity_mismatch_is_never_signalled(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "native-pid.json").write_text(
        json.dumps({"pid": 4242, "start": "live-identity"})
    )
    session = NativeSession(
        Settings(retain_diagnostics=False),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=SimpleNamespace(close=lambda: None),
    )
    session.runtime = runtime
    session._tmux = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr(
        "claude_native_bridge.native.process_start", lambda pid: "live-identity"
    )
    monkeypatch.setattr("claude_native_bridge.native.os.getpgid", lambda pid: pid + 1)
    monkeypatch.setattr("claude_native_bridge.native.os.kill", lambda pid, sig: None)
    monkeypatch.setattr(
        "claude_native_bridge.native.os.killpg",
        lambda *args: pytest.fail("mismatched process group was signalled"),
    )

    with pytest.raises(NativeBridgeError, match="process group mismatch"):
        session.close()

    assert not session.cleanup_outcome.safe_to_release_capacity
    assert runtime.exists()


def test_launch_receipt_without_pid_is_retained_as_physically_uncertain(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("claude_native_bridge.native.sys.platform", "win32")
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "launch.json").write_text("{}")
    session = NativeSession(
        Settings(retain_diagnostics=False),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=SimpleNamespace(close=lambda: None),
    )
    session.runtime = runtime
    session._tmux = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1)

    outcome = session.close()

    assert not outcome.safe_to_release_capacity
    assert outcome.diagnostics_retained
    assert runtime.exists()


def test_windows_cleanup_uses_controller_job_liveness_not_posix_signals(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "launch.json").write_text("{}")
    terminal = SimpleNamespace(alive=True)
    terminal.is_alive = lambda: terminal.alive

    class Controller:
        def __init__(self):
            self.terminal = terminal
            self.calls = []

        def command(self, action, *args, check=True):
            self.calls.append(action)
            if action == "kill-server":
                terminal.alive = False
            return subprocess.CompletedProcess((action, *args), 0 if terminal.alive else 1)

    controller = Controller()
    session = NativeSession(
        Settings(retain_diagnostics=True),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=SimpleNamespace(close=lambda: None),
    )
    session.runtime = runtime
    session._windows_controller = controller
    monkeypatch.setattr("claude_native_bridge.native.sys.platform", "win32")

    outcome = session.close()

    assert controller.calls == ["kill-server", "has-session"]
    assert outcome.process_dead
    assert outcome.safe_to_release_capacity
