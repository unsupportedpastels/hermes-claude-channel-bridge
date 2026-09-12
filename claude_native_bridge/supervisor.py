"""Native process lifetime follows its owning Hermes process and a bounded lease."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


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
    (runtime / "native-pid.json").write_text(json.dumps({"pid": process.pid}))
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
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        # Kill any grandchildren left after their leader exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return process.returncode


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1]))
