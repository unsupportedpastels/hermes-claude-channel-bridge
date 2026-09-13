"""Interactive Windows ConPTY backend; native imports are deliberately lazy.

A gated Python bootstrap is assigned to a kill-on-close Job before it can spawn
Claude. This closes the usual spawn/AssignProcessToJobObject descendant race.
No shell or non-interactive inference API is used. A built-in CreateProcess with
CREATE_NEW_CONSOLE (or unredirected PowerShell Start-Process) is sufficient for
manual consent and MCP task data, but does not expose the separate console's
screen/input to this parent. This backend retains ConPTY specifically for the
existing automatic capture/send_key consent contract and background operation;
it is not required for MCP itself. Git Bash adds no needed launch capability.
Neither launch route has been verified on a Windows target yet.

The runtime must already be
protected by secure_runtime_directory(), before any config/secrets are written.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

_KEYS = {
    "Enter": "\r",
    "Down": "\x1b[B",
    "Up": "\x1b[A",
    "Left": "\x1b[D",
    "Right": "\x1b[C",
    "Escape": "\x1b",
    "Tab": "\t",
    "C-c": "\x03",
}


def key_sequence(key: str) -> str:
    try:
        return _KEYS[key]
    except (KeyError, TypeError):
        raise ValueError("Unsupported terminal key") from None


def windows_command_line(argv: list[str]) -> str:
    if (
        not isinstance(argv, list)
        or not argv
        or not argv[0]
        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
    ):
        raise ValueError("Expected nonempty argv list without NULs")
    return subprocess.list2cmdline(argv)


class WindowsTerminal:
    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        runtime: Path,
        owner_pid: int,
    ):
        windows_command_line(argv)
        if Path(argv[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("Use the native executable, not a shell shim")
        if type(owner_pid) is not int or owner_pid <= 0:
            raise ValueError("owner_pid must be a positive process id")
        if any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not k
            or "=" in k
            or "\0" in k
            or "\0" in v
            for k, v in env.items()
        ):
            raise ValueError("Invalid environment")
        self.argv, self.cwd, self.env = list(argv), Path(cwd), dict(env)
        self.runtime, self.owner_pid = Path(runtime), owner_pid
        self.pid = None  # job-root bootstrap; Claude and MCP are its descendants
        self._pty = self._job = self._owner = self._gate = None
        self._screen = self._reader = self._watcher = None
        self._config = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._started = False
        self._error = None

    def start(self):
        if sys.platform != "win32":
            raise OSError("ConPTY requires native Windows")
        import pyte
        import win32api
        import win32con
        import win32event
        import win32job
        from winpty import PTY
        from winpty.enums import Backend

        with self._lock:
            if self._started or self._closed:
                raise RuntimeError("Terminal is single-use")
            self._started = True
            try:
                self._owner = win32api.OpenProcess(
                    win32con.SYNCHRONIZE, False, self.owner_pid
                )
                if (
                    win32event.WaitForSingleObject(self._owner, 0)
                    != win32event.WAIT_TIMEOUT
                ):
                    raise RuntimeError("Terminal owner already exited")
                # pywin32 311 rejects None for the optional name even though older
                # releases accepted it. Empty string still creates an unnamed job.
                self._job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation
                )
                limits["BasicLimitInformation"]["LimitFlags"] = (
                    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                )
                win32job.SetInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation, limits
                )
                gate_name = "Local\\hcb-" + uuid.uuid4().hex
                self._gate = win32event.CreateEvent(None, True, False, gate_name)
                self._config = self.runtime / ("terminal-" + uuid.uuid4().hex + ".json")
                with self._config.open("x", encoding="utf-8") as file:
                    json.dump({"argv": self.argv, "gate": gate_name}, file)
                # -I prevents cwd/PYTHONPATH startup injection before job assignment.
                bootstrap = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    str(self._config),
                ]
                self._pty = PTY(160, 45, backend=Backend.ConPTY)
                environment = (
                    "\0".join(f"{k}={v}" for k, v in sorted(self.env.items())) + "\0\0"
                )
                self._pty.spawn(
                    bootstrap[0],
                    cwd=str(self.cwd),
                    env=environment,
                    cmdline=" " + windows_command_line(bootstrap[1:]),
                )
                self.pid = self._pty.pid
                process = win32api.OpenProcess(
                    win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                    False,
                    self.pid,
                )
                try:
                    win32job.AssignProcessToJobObject(self._job, process)
                except BaseException:
                    win32api.TerminateProcess(process, 1)
                    raise
                finally:
                    process.Close()
                self._screen = pyte.Screen(160, 45)
                self._screen.write_process_input = self._pty.write
                stream = pyte.Stream(self._screen)
                self._reader = threading.Thread(
                    target=self._read,
                    args=(stream,),
                    daemon=True,
                    name="hcb-conpty-reader",
                )
                self._reader.start()
                self._watcher = threading.Thread(
                    target=self._watch_owner, daemon=True, name="hcb-conpty-owner"
                )
                self._watcher.start()
                if (
                    win32event.WaitForSingleObject(self._owner, 0)
                    != win32event.WAIT_TIMEOUT
                ):
                    raise RuntimeError("Terminal owner exited during startup")
                win32event.SetEvent(self._gate)
                return self
            except BaseException:
                self.close()
                raise

    def _read(self, stream):
        try:
            while not self._stop.is_set():
                text = self._pty.read(blocking=False)
                if text:
                    with self._lock:
                        stream.feed(text)
                elif not self._pty.isalive():
                    return
                self._stop.wait(0.02)
        except Exception as exc:  # noqa: BLE001 - native reader failures must close the job
            if not self._stop.is_set():
                self._error = exc
                self.close()

    def _watch_owner(self):
        import win32event

        while not self._stop.wait(0.1):
            with self._lock:
                if self._stop.is_set():
                    return
                result = win32event.WaitForSingleObject(self._owner, 0)
            if result != win32event.WAIT_TIMEOUT:
                self.close()
                return

    def capture(self) -> str:
        with self._lock:
            if self._error is not None:
                raise RuntimeError("ConPTY capture failed") from self._error
            return (
                "\n".join(line.rstrip() for line in self._screen.display)
                if self._screen
                else ""
            )

    def send_key(self, key: str):
        sequence = key_sequence(key)
        with self._lock:
            if not self.is_alive():
                raise RuntimeError("Terminal is not running")
            self._pty.write(sequence)

    def is_alive(self) -> bool:
        with self._lock:
            return bool(
                not self._closed and self._pty is not None and self._pty.isalive()
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            if self._job is not None:
                import win32job

                try:
                    win32job.TerminateJobObject(self._job, 1)
                finally:
                    self._job.Close()
                    self._job = None
            for name in ("_gate", "_owner"):
                handle = getattr(self, name)
                if handle is not None:
                    handle.Close()
                    setattr(self, name, None)
            if self._config is not None:
                self._config.unlink(missing_ok=True)
        # No join while holding the screen/lifecycle lock; no unbounded IO wait.
        for thread in (self._reader, self._watcher):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=0.5)
        self._pty = None


def _bootstrap(config_path: str) -> int:
    """Do not spawn any descendants until the parent confirms job assignment."""
    import win32con
    import win32event

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    gate = win32event.OpenEvent(win32con.SYNCHRONIZE, False, config["gate"])
    try:
        if win32event.WaitForSingleObject(gate, 15000) != win32event.WAIT_OBJECT_0:
            return 1
    finally:
        gate.Close()
    Path(config_path).unlink(missing_ok=True)
    return subprocess.call(config["argv"], shell=False)


if __name__ == "__main__":
    raise SystemExit(_bootstrap(sys.argv[1]))
