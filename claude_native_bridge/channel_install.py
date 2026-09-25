"""Locked npm installation for the bundled channel runtime.

Hermes' plugin installer clones and enables a plugin but never runs npm, so
setup installs the pinned channel dependencies itself when they are missing.
Only the locked ``npm ci`` form is used; scripts stay disabled.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .diagnostics import _channel_dependency_checks
from .settings import NativeBridgeError

NPM_ARGUMENTS = ("ci", "--ignore-scripts", "--no-audit", "--no-fund")
INSTALL_TIMEOUT_SECONDS = 600
SETUP_COMMAND = "hermes-claude-bridge setup --accept-development-channels"


def channel_root() -> Path:
    return Path(__file__).resolve().parent / "channel"


def channel_dependencies_ready(root: str | Path | None = None) -> bool:
    """True when every pinned channel dependency matches the doctor contract."""
    root = channel_root() if root is None else Path(root)
    return all(check["status"] == "pass" for check in _channel_dependency_checks(root))


def install_command(root: str | Path | None = None, *, npm: str = "npm") -> list[str]:
    root = channel_root() if root is None else Path(root)
    return [npm, "--prefix", str(root), *NPM_ARGUMENTS]


def install_channel_dependencies(
    root: str | Path | None = None,
    *,
    executable_resolver: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> dict:
    """Run the locked npm install for the channel package and verify the result."""
    root = channel_root() if root is None else Path(root)
    manual = shlex.join(install_command(root))
    if not (root / "package-lock.json").is_file():
        raise NativeBridgeError(
            f"Channel package-lock.json is missing from {root}; the installed package is incomplete"
        )
    npm = executable_resolver("npm")
    if not npm:
        raise NativeBridgeError(
            "npm was not found on PATH; install Node 22 or newer with npm, then rerun setup or run: "
            + manual
        )
    argv = install_command(root, npm=npm)
    try:
        completed = runner(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeBridgeError(
            f"npm ci could not be run ({type(exc).__name__}); run manually: {manual}"
        ) from exc
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise NativeBridgeError(
            f"npm ci exited with status {completed.returncode}; run manually: {manual}"
            + (f"\n{tail}" if tail else "")
        )
    if not channel_dependencies_ready(root):
        raise NativeBridgeError(
            "Channel dependencies still do not match the pinned contract after npm ci; "
            "inspect the channel directory and rerun: " + manual
        )
    return {"installed": True, "command": argv, "root": str(root)}
