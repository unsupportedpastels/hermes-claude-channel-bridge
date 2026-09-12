"""Portable contracts plus real Windows-only ConPTY/ACL checks (no inference)."""

import os
import sys
import time

import pytest

from claude_native_bridge.windows_security import secure_runtime_directory
from claude_native_bridge.windows_terminal import (
    WindowsTerminal,
    key_sequence,
    windows_command_line,
)


def test_arguments_preserve_empty_unicode_quotes_and_spaces():
    import subprocess

    args = ["C:\\Program Files\\Claude\\claude.exe", "", "hello 世界", 'a"b', "x\\"]
    assert windows_command_line(args) == subprocess.list2cmdline(args)


@pytest.mark.parametrize("args", [[], [""], ["ok", "\x00"], "not-a-list"])
def test_invalid_arguments(args):
    with pytest.raises(ValueError):
        windows_command_line(args)


def test_terminal_rejects_shell_shims(tmp_path):
    for command in ["claude.cmd", "CLAUDE.BAT"]:
        with pytest.raises(ValueError, match="native executable"):
            WindowsTerminal(
                argv=[command],
                cwd=tmp_path,
                env={},
                runtime=tmp_path,
                owner_pid=os.getpid(),
            )


def test_keys_are_explicit_not_shell_text():
    assert key_sequence("Enter") == "\r"
    assert key_sequence("Down") == "\x1b[B"
    assert key_sequence("C-c") == "\x03"
    with pytest.raises(ValueError):
        key_sequence("echo injected")


def test_safe_import_and_unstarted_close(tmp_path):
    terminal = WindowsTerminal(
        argv=["python"], cwd=tmp_path, env={}, runtime=tmp_path, owner_pid=os.getpid()
    )
    assert not terminal.is_alive()
    assert terminal.capture() == ""
    terminal.close()
    terminal.close()
    if sys.platform != "win32":
        with pytest.raises(OSError):
            secure_runtime_directory(tmp_path)


windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="requires real Windows ConPTY and ACLs"
)


@windows_only
def test_real_conpty_input_capture_and_descendant_cleanup(tmp_path):
    import win32api
    import win32con
    import win32event

    runtime = tmp_path / "runtime"
    secure_runtime_directory(runtime)
    code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('CHILD:'+str(p.pid),flush=True); print('READY',flush=True); input(); print('ACCEPTED',flush=True); input()"
    terminal = WindowsTerminal(
        argv=[sys.executable, "-u", "-c", code],
        cwd=runtime,
        env=dict(os.environ),
        runtime=runtime,
        owner_pid=os.getpid(),
    )
    child = None
    try:
        terminal.start()
        deadline = time.monotonic() + 15
        while "READY" not in terminal.capture() and time.monotonic() < deadline:
            time.sleep(0.05)
        screen = terminal.capture()
        assert "READY" in screen
        import re

        pid = int(re.search(r"CHILD:(\d+)", screen)[1])
        child = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        terminal.send_key("Enter")
        deadline = time.monotonic() + 5
        while "ACCEPTED" not in terminal.capture() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "ACCEPTED" in terminal.capture()
    finally:
        terminal.close()
    assert not terminal.is_alive()
    assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
    child.Close()


@windows_only
def test_owner_exit_closes_job(tmp_path):
    import subprocess

    import win32api
    import win32con
    import win32event

    runtime = tmp_path / "runtime"
    secure_runtime_directory(runtime)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    terminal = WindowsTerminal(
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=runtime,
        env=dict(os.environ),
        runtime=runtime,
        owner_pid=owner.pid,
    )
    root = None
    try:
        terminal.start()
        root = win32api.OpenProcess(win32con.SYNCHRONIZE, False, terminal.pid)
        owner.terminate()
        owner.wait(timeout=5)
        assert win32event.WaitForSingleObject(root, 5000) == win32event.WAIT_OBJECT_0
        deadline = time.monotonic() + 5
        while terminal.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not terminal.is_alive()
    finally:
        terminal.close()
        if root is not None:
            root.Close()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@windows_only
def test_windows_acl_owner_only_and_reparse_rejection(tmp_path):
    import win32security

    runtime = tmp_path / "runtime"
    secure_runtime_directory(runtime)
    sd = win32security.GetNamedSecurityInfo(
        str(runtime),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    dacl = sd.GetSecurityDescriptorDacl()
    assert dacl.GetAceCount() == 1
    assert dacl.GetAce(0)[2] == sd.GetSecurityDescriptorOwner()
    link = tmp_path / "junction"
    import subprocess

    result = subprocess.run(
        [
            os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"),
            "/c",
            "mklink",
            "/J",
            str(link),
            str(runtime),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, "Could not create junction fixture"
    try:
        with pytest.raises(ValueError, match="reparse"):
            secure_runtime_directory(link)
    finally:
        link.rmdir()
    (runtime / "secret").write_text("test")
    with pytest.raises(ValueError):
        secure_runtime_directory(runtime)
