import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

try:
    import pty
except ImportError:
    pty = None
import select
import signal
import unittest
from unittest.mock import patch
import shlex
import shutil
import threading
from claude_native_bridge.native import NativeSession
from claude_native_bridge.settings import Settings
from claude_native_bridge.native import process_start
from claude_native_bridge import supervisor


class ProcessIdentityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ("linux", "darwin"), "Unix identity backend")
    def test_live_process_identity_is_shared_and_stable(self):
        identity = process_start(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity, supervisor.process_start(os.getpid()))
        self.assertEqual(identity, process_start(os.getpid()))

    def test_invalid_or_missing_pid_has_no_identity(self):
        for pid in (None, True, -1, 0, "1", 2**40):
            with self.subTest(pid=pid):
                self.assertIsNone(process_start(pid))


@unittest.skipUnless(sys.platform in ("linux", "darwin"), "Unix supervisor")
class SupervisorTests(unittest.TestCase):
    def test_unidentifiable_owner_fails_before_native_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "launch.json").write_text(json.dumps({"owner_start": None}))
            with patch.object(supervisor.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "owner identity"):
                    supervisor.supervise(folder)
                spawn.assert_not_called()

    def test_native_child_keeps_controlling_terminal_for_tui_input(self):
        supervisor = (
            Path(__file__).resolve().parents[1]
            / "claude_native_bridge"
            / "supervisor.py"
        )
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            code = "import os; print('TTY_OK' if os.tcgetpgrp(0)==os.getpgrp() else 'TTY_WRONG', flush=True)"
            (p / "launch.json").write_text(
                json.dumps(
                    {
                        "argv": [sys.executable, "-c", code],
                        "environment": dict(os.environ),
                        "owner_pid": os.getpid(),
                        "owner_start": process_start(os.getpid()),
                    }
                )
            )
            (p / "lease.json").write_text(json.dumps({"expires": time.time() + 10}))
            assert pty is not None
            pid, fd = pty.fork()
            if pid == 0:
                os.execv(sys.executable, [sys.executable, str(supervisor), folder])
            data = b""
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if select.select([fd], [], [], 0.2)[0]:
                        try:
                            part = os.read(fd, 4096)
                        except OSError:
                            break
                        if not part:
                            break
                        data += part
                self.assertIn(b"TTY_OK", data, data.decode(errors="replace"))
            finally:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)

    def test_lost_owner_or_expired_lease_reaps_native_child(self):
        supervisor = (
            Path(__file__).resolve().parents[1]
            / "claude_native_bridge"
            / "supervisor.py"
        )
        for lost_owner in (True, False):
            with (
                self.subTest(lost_owner=lost_owner),
                tempfile.TemporaryDirectory() as folder,
            ):
                p = Path(folder)
                (p / "launch.json").write_text(
                    json.dumps(
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                "import time; time.sleep(60)",
                            ],
                            "environment": dict(os.environ),
                            "owner_pid": os.getpid(),
                            "owner_start": "not-this-process"
                            if lost_owner
                            else process_start(os.getpid()),
                        }
                    )
                )
                (p / "lease.json").write_text(
                    json.dumps(
                        {"expires": time.time() + 30 if lost_owner else time.time() - 1}
                    )
                )
                run = subprocess.run(
                    [sys.executable, str(supervisor), folder], timeout=8
                )
                self.assertNotEqual(run.returncode, 0)
                pid = json.loads((p / "native-pid.json").read_text())["pid"]
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


@unittest.skipUnless(
    sys.platform in ("linux", "darwin") and shutil.which("tmux"),
    "requires a Unix tmux installation",
)
class TmuxLifecycleTests(unittest.TestCase):
    def test_dedicated_tty_and_close_idle_cancellation_cleanup(self):
        for mode in ("close", "idle", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                session = NativeSession(
                    Settings(idle_timeout=0.1),
                    Path(folder),
                    "claude-sonnet-5",
                    "low",
                    http_client=object(),
                )
                session.runtime = Path(folder) / "runtime"
                session.runtime.mkdir()
                receipt = Path(folder) / "tty.json"
                code = (
                    "import os,json,time; from pathlib import Path; "
                    f"Path({str(receipt)!r}).write_text(json.dumps(dict("
                    "pid=os.getpid(),tty=os.isatty(0),"
                    "foreground=os.tcgetpgrp(0)==os.getpgrp()))); time.sleep(60)"
                )
                session._private_json(
                    "launch.json",
                    {
                        "argv": [sys.executable, "-c", code],
                        "environment": dict(os.environ),
                        "owner_pid": os.getpid(),
                        "owner_start": process_start(os.getpid()),
                    },
                )
                session._heartbeat()
                pid = None
                try:
                    session._tmux(
                        "new-session",
                        "-d",
                        "-s",
                        "worker",
                        shlex.join(
                            [sys.executable, supervisor.__file__, str(session.runtime)]
                        ),
                    )
                    deadline = time.monotonic() + 5
                    while not receipt.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    data = json.loads(receipt.read_text())
                    pid = data["pid"]
                    self.assertTrue(data["tty"])
                    self.assertTrue(data["foreground"])
                    if mode == "idle":
                        session._last_used = time.monotonic() - 1
                        watcher = threading.Thread(target=session._idle_watch)
                        watcher.start()
                        watcher.join(timeout=8)
                        self.assertFalse(watcher.is_alive())
                    elif mode == "cancel":
                        # Only transport is stubbed; cancellation tears down the
                        # actual tmux/supervisor/native process tree.
                        with patch.object(session, "_api", return_value={}):
                            with self.assertRaises(InterruptedError):
                                session.exchange("unused", "cancel-test", lambda: True)
                    else:
                        session.close()
                    self.assertTrue(session.closed)
                    self.assertFalse(session.runtime.exists())
                    self.assertNotEqual(
                        session._tmux("has-session", check=False).returncode, 0
                    )
                    # tmux can exit before macOS launchd reaps the orphaned
                    # child. Bound that asynchronous reap instead of requiring
                    # kill(pid, 0) to fail in the very same scheduling instant.
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                finally:
                    session.close()


if __name__ == "__main__":
    unittest.main()
