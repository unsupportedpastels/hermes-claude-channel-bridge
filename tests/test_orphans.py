import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claude_native_bridge import api_server, supervisor


@unittest.skipUnless(os.name == "posix", "orphan sweep is Unix-only")
class OrphanSweepTests(unittest.TestCase):
    def setUp(self):
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def _spawn(self, command):
        process = subprocess.Popen(command, start_new_session=True)
        self.processes.append(process)
        return process

    def _run_dir(self, home, name, pid, session_id=None):
        run = home / "claude-native-bridge" / "runs" / name
        run.mkdir(parents=True)
        (run / "native-pid.json").write_text(json.dumps({"pid": pid}))
        if session_id is not None:
            (run / "launch.json").write_text(
                json.dumps({"session_id": session_id})
            )
        return run

    def test_sweep_terminates_verified_claude_group_and_archives_run(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            claude = home / "claude-fixture"
            sleep = shutil.which("sleep")
            if sleep is None:
                self.fail("sleep executable is required")
            claude.symlink_to(sleep)
            process = self._spawn([str(claude), "60"])
            run = self._run_dir(home, "session-live", process.pid, "fixture")

            archived = supervisor.sweep_orphaned_runs(home)

            process.wait(timeout=3)
            self.assertFalse(run.exists())
            self.assertEqual(archived, [run.with_name("session-live.archived")])
            self.assertTrue(archived[0].is_dir())

    def test_sweep_archives_stale_pid_without_signalling(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            process = self._spawn(["sleep", "60"])
            pid = process.pid
            process.terminate()
            process.wait(timeout=3)
            run = self._run_dir(home, "session-stale", pid)

            archived = supervisor.sweep_orphaned_runs(home)

            self.assertEqual(archived, [run.with_name("session-stale.archived")])
            self.assertTrue(archived[0].is_dir())

    def test_pid_reuse_guard_does_not_kill_non_claude_process(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            process = self._spawn(["sleep", "60"])
            run = self._run_dir(home, "session-reused", process.pid)

            archived = supervisor.sweep_orphaned_runs(home)

            self.assertIsNone(process.poll())
            self.assertEqual(archived, [run.with_name("session-reused.archived")])

    def test_main_sweeps_before_constructing_api_app(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            token = home / "token"
            ready = home / "ready.json"
            token.write_text("fixture-token")
            events = []

            def sweep(path):
                self.assertEqual(path, home)
                events.append("sweep")

            def create_app(*args, **kwargs):
                self.assertEqual(kwargs, {"owner_limit": 2})
                events.append("app")
                return object()

            with (
                patch.object(api_server, "sweep_orphaned_runs", sweep),
                patch("claude_native_bridge.api.create_app", create_app),
                patch("uvicorn.Server.run", return_value=None),
            ):
                api_server.main(
                    [
                        "--home",
                        str(home),
                        "--token-file",
                        str(token),
                        "--ready-file",
                        str(ready),
                    ]
                )

            self.assertEqual(events, ["sweep", "app"])


if __name__ == "__main__":
    unittest.main()
