"""Pure native-platform launch policy shared by the API and CLI controllers."""

import base64
import os
import shlex
import sys

_UNIX_ENV = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
}
_WINDOWS_ENV = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
}


def native_environment(source=None, *, platform=None):
    source = os.environ if source is None else source
    platform = sys.platform if platform is None else platform
    allowed = _UNIX_ENV | (_WINDOWS_ENV if platform == "win32" else set())
    env = {k: v for k, v in source.items() if k.upper() in allowed and v}
    if platform == "win32" and not env.get("HOME"):
        profile = next(
            (v for k, v in source.items() if k.upper() == "USERPROFILE"), None
        )
        if profile:
            env["HOME"] = profile
    env["TERM"] = "xterm-256color"
    env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] = "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def script_command(executable, script, runtime, *, platform=None):
    """A local hook command, not the native inference process's console."""
    platform = sys.platform if platform is None else platform
    args = [str(executable), str(script), str(runtime)]
    if platform != "win32":
        return shlex.join(args)
    quoted = ["'" + value.replace("'", "''") + "'" for value in args]
    command = (
        "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$payload = [Console]::In.ReadToEnd(); $payload | & " + " ".join(quoted)
    )
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
