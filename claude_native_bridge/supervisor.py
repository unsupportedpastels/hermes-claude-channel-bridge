"""Native process lifetime follows its owning Hermes process and a bounded lease."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _pid_is_live(pid):
    """Return liveness without relying on Linux-only process files."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps_identity(pid):
    """Return a stable-enough ps fingerprint and process group for a live PID."""
    if not _pid_is_live(pid):
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pgid=", "-o", "lstart=", "-o", "args="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    if result.returncode or not output:
        return None
    fields = output.split(maxsplit=1)
    if len(fields) != 2:
        return None
    try:
        process_group = int(fields[0])
    except ValueError:
        return None
    return process_group, output


def _native_identity(pid):
    identity = _ps_identity(pid)
    if identity is None or "claude" not in identity[1].lower():
        return None
    # NativeSession launches the CLI as leader of its own process group. Refuse
    # to signal any other group: a mismatched group is stale/reused metadata.
    if identity[0] != pid:
        return None
    return identity


def _kill_tmux_server(runtime):
    try:
        launch = json.loads((runtime / "launch.json").read_text())
    except (OSError, ValueError):
        return
    session_id = launch.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        return
    try:
        subprocess.run(
            ["tmux", "-L", "hcb-" + session_id, "kill-server"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_native(pid, start, identity):
    """Terminate only while PID, start time, argv, and process group still match."""
    for sig, seconds in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
        if process_start(pid) != start or _native_identity(pid) != identity:
            return
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process_start(pid) != start or _ps_identity(pid) != identity:
                return
            time.sleep(0.05)


def _signal_owned_group(pid, start, sig):
    """Signal a launched child group only while its recorded leader still owns it."""
    if start is None or process_start(pid) != start:
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _archive_run(runtime):
    """Archive by directory rename so startup will never sweep the run twice."""
    target = runtime.with_name(runtime.name + ".archived")
    if target.exists():
        target = runtime.with_name(runtime.name + f".archived-{time.time_ns()}")
    runtime.rename(target)
    return target


def sweep_orphaned_runs(home):
    """Kill verified orphaned Claude groups and archive every prior run directory."""
    runs = Path(home) / "claude-native-bridge" / "runs"
    if not runs.is_dir():
        return []
    archived = []
    for runtime in sorted(runs.glob("session-*")):
        if not runtime.is_dir() or ".archived" in runtime.name:
            continue
        try:
            receipt = json.loads((runtime / "native-pid.json").read_text())
            pid = receipt.get("pid")
            start = receipt.get("start")
        except (OSError, ValueError):
            pid = None
            start = None
        identity = None
        if isinstance(start, str) and start and process_start(pid) == start:
            identity = _native_identity(pid)
        if identity is not None:
            _kill_tmux_server(runtime)
            _terminate_native(pid, start, identity)
        archived.append(_archive_run(runtime))
    return archived


def _darwin_process_start(pid):
    # libproc exposes microsecond-resolution creation time. `ps lstart` only
    # has second precision and can mistake a rapidly reused PID for its owner.
    import ctypes

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = (
            [
                (name, ctypes.c_uint32)
                for name in (
                    "flags",
                    "status",
                    "xstatus",
                    "pid",
                    "ppid",
                    "uid",
                    "gid",
                    "ruid",
                    "rgid",
                    "svuid",
                    "svgid",
                    "rfu_1",
                )
            ]
            + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
            + [
                (name, ctypes.c_uint32)
                for name in (
                    "nfiles",
                    "pgid",
                    "pjobc",
                    "e_tdev",
                    "e_tpgid",
                )
            ]
            + [
                ("nice", ctypes.c_int32),
                ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64),
            ]
        )

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        query = libproc.proc_pidinfo
        query.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        query.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        if query(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid:
            return None
        return f"{info.start_sec}:{info.start_usec}"
    except (OSError, AttributeError):
        return None


def process_start(pid):
    """Stable Unix process identity, or None when it cannot be established."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return None
    if sys.platform == "darwin":
        return _darwin_process_start(pid)
    if sys.platform != "linux":
        return None
    try:
        return (
            Path("/proc/" + str(pid) + "/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19]
        )
    except (OSError, IndexError):
        return None


def supervise(runtime):
    runtime = Path(runtime)
    spec = json.loads((runtime / "launch.json").read_text())
    if not spec.get("owner_start"):
        raise RuntimeError("Cannot supervise a native process without owner identity")

    def claim_terminal():
        # This supervisor is a standalone single-threaded process. Keep the
        # controlling tty, but give the native tree its own killable group.
        if os.isatty(0):
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            os.tcsetpgrp(0, os.getpgrp())
            signal.signal(signal.SIGTTOU, signal.SIG_DFL)

    process = subprocess.Popen(
        spec["argv"],
        env=spec["environment"],
        process_group=0,
        preexec_fn=claim_terminal,
    )
    child_start = process_start(process.pid)
    (runtime / "native-pid.json").write_text(
        json.dumps({"pid": process.pid, "start": child_start})
    )
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while process.poll() is None and not stopping:
            if process_start(spec["owner_pid"]) != spec["owner_start"]:
                break
            try:
                lease = json.loads((runtime / "lease.json").read_text())
            except (OSError, ValueError):
                break
            if time.time() >= lease["expires"]:
                break
            time.sleep(0.2)
    finally:
        # The private process group includes the native CLI and its channel child.
        _signal_owned_group(process.pid, child_start, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _signal_owned_group(process.pid, child_start, signal.SIGKILL)
            process.wait(timeout=3)
        # Kill any grandchildren left after their leader exited.
        _signal_owned_group(process.pid, child_start, signal.SIGKILL)
    return process.returncode


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1]))
