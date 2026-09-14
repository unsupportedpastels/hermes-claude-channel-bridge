import json
import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

from claude_native_bridge import native, supervisor
from claude_native_bridge.native import NativeSession
from claude_native_bridge.settings import Settings


def _session(tmp_path, receipt):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "native-pid.json").write_text(json.dumps(receipt))
    session = NativeSession(
        Settings(retain_diagnostics=False),
        tmp_path,
        "claude-sonnet-5",
        "medium",
        http_client=SimpleNamespace(close=lambda: None),
    )
    session.runtime = runtime
    session._tmux = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1)
    return session, runtime


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_supervisor_persists_child_start_identity_at_launch(tmp_path, monkeypatch):
    (tmp_path / "launch.json").write_text(
        json.dumps(
            {
                "argv": ["claude"],
                "environment": {},
                "owner_pid": 12,
                "owner_start": "owner-start",
            }
        )
    )

    process = SimpleNamespace(pid=4242, returncode=0)
    process.poll = lambda: 0
    process.wait = lambda timeout: 0
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(supervisor, "process_start", lambda pid: "child-start")
    monkeypatch.setattr(supervisor.os, "killpg", lambda *args: None)

    assert supervisor.supervise(tmp_path) == 0
    assert json.loads((tmp_path / "native-pid.json").read_text()) == {
        "pid": 4242,
        "start": "child-start",
    }


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_recycled_pid_is_not_signalled_and_proves_original_process_gone(
    tmp_path, monkeypatch
):
    session, runtime = _session(tmp_path, {"pid": 4242, "start": "original-start"})
    monkeypatch.setattr(native, "process_start", lambda pid: "reused-start")
    monkeypatch.setattr(native.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        native.os,
        "killpg",
        lambda *args: pytest.fail("recycled PID was signalled"),
    )

    outcome = session.close()

    assert outcome.process_dead
    assert outcome.safe_to_release_capacity
    assert not outcome.process_identity_verified
    assert not runtime.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_valid_current_pid_identity_is_terminated_and_verified(tmp_path, monkeypatch):
    session, runtime = _session(tmp_path, {"pid": 4242, "start": "child-start"})
    identities = iter(["child-start", "child-start", None, None])
    monkeypatch.setattr(native, "process_start", lambda pid: next(identities, None))
    monkeypatch.setattr(native.os, "getpgid", lambda pid: pid)
    signals = []
    monkeypatch.setattr(native.os, "killpg", lambda pid, sig: signals.append(sig))

    outcome = session.close()

    assert signals == [signal.SIGTERM]
    assert outcome.process_identity_verified
    assert outcome.process_dead
    assert outcome.safe_to_release_capacity
    assert not runtime.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_missing_legacy_identity_cannot_authorize_signal_or_prove_live_pid_dead(
    tmp_path, monkeypatch
):
    session, runtime = _session(tmp_path, {"pid": 4242})
    monkeypatch.setattr(
        native,
        "process_start",
        lambda pid: pytest.fail("legacy receipt was treated as an identity"),
    )
    monkeypatch.setattr(native.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        native.os,
        "killpg",
        lambda *args: pytest.fail("legacy PID-only receipt authorized a signal"),
    )

    outcome = session.close()

    assert not outcome.process_identity_verified
    assert not outcome.process_dead
    assert not outcome.safe_to_release_capacity
    assert runtime.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"),
    reason="requires POSIX process-group signals",
)
def test_early_child_exit_with_missing_start_identity_is_verified_absent(
    tmp_path, monkeypatch
):
    session, runtime = _session(tmp_path, {"pid": 4242, "start": None})

    def absent(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(native.os, "kill", absent)
    monkeypatch.setattr(
        native.os,
        "killpg",
        lambda *args: pytest.fail("absent child was signalled"),
    )

    outcome = session.close()

    assert not outcome.process_identity_verified
    assert outcome.process_dead
    assert outcome.safe_to_release_capacity
    assert not runtime.exists()


def test_orphan_sweep_rejects_recycled_claude_pid(tmp_path, monkeypatch):
    run = tmp_path / "claude-native-bridge" / "runs" / "session-recycled"
    run.mkdir(parents=True)
    (run / "native-pid.json").write_text(
        json.dumps({"pid": 4242, "start": "original-start"})
    )
    monkeypatch.setattr(supervisor, "process_start", lambda pid: "reused-start")
    monkeypatch.setattr(
        supervisor,
        "_native_identity",
        lambda pid: (pid, "claude fixture"),
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_native",
        lambda *args: pytest.fail("recycled orphan PID was signalled"),
    )

    archived = supervisor.sweep_orphaned_runs(tmp_path)

    assert archived == [run.with_name("session-recycled.archived")]
