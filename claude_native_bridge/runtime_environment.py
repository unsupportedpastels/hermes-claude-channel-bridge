"""Isolated Python runtime for the plugin-owned API server.

The provider adapter runs inside Hermes on Hermes's own dependencies. The API
server runs in a separate environment built from ``SERVER_REQUIREMENTS`` by
Hermes's package manager (PM), so its interpreter is whichever Python PM pins:
the plugin selects no Python version and adds nothing to Hermes's dependency
set. Reading the runtime never installs; only ``provision`` does, and only
when a caller explicitly asks for it.

The server is started in isolated mode (``-I``) with only this package made
importable, so ambient PYTHONPATH, VIRTUAL_ENV, user site-packages and the
host's dependency generation cannot leak into it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ENVIRONMENT_NAME = "claude-native-bridge-server"

# Server-only dependencies: minimums only, never upper bounds. Keep in step
# with the ``server`` extra in pyproject.toml; a test enforces the match.
SERVER_REQUIREMENTS = (
    "PyYAML>=6",
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "jsonschema>=4",
    "httpx>=0.27",
    "psutil>=5.9",
    "filelock>=3.15",
    "pywinpty>=2.0.15; sys_platform == 'win32'",
    "pywin32>=308; sys_platform == 'win32'",
    "pyte>=0.8.2; sys_platform == 'win32'",
)
REPAIR_COMMAND = "hermes-claude-bridge repair"
PROVISION_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 120
PROBE_MODULE = "claude_native_bridge._runtime_probe"
SERVER_MODULE = "claude_native_bridge.api_server"
MARKER = "bridge-runtime.json"

# One line, no double quotes: it must survive Windows command-line quoting and
# the venv redirector unchanged, because stop/start compare argv exactly.
_BOOTSTRAP = (
    "import importlib.util as u, os, runpy, sys; "
    "p = sys.argv.pop(1); "
    "s = u.spec_from_file_location('claude_native_bridge', "
    "os.path.join(p, '__init__.py'), submodule_search_locations=[p]); "
    "m = u.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m); "
    "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
)
_AMBIENT = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONSAFEPATH",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
)
_ERROR_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)\b.*$")


class RuntimeUnavailable(RuntimeError):
    """The server runtime is missing, outdated or unusable."""


def package_directory() -> Path:
    return Path(__file__).resolve().parent


def environment_root(home) -> Path:
    from .api_config import api_storage

    # Shared with the API store so homes that share one server share its runtime.
    return api_storage(home).parent / "server-environment"


def requirements_digest() -> str:
    return hashlib.sha256("\n".join(SERVER_REQUIREMENTS).encode()).hexdigest()


def command(python, module, *args) -> list[str]:
    """Run ``module`` from this package under ``python`` in isolated mode."""
    return [
        str(python),
        "-I",
        "-X",
        "utf8",
        "-c",
        _BOOTSTRAP,
        str(package_directory()),
        module,
        *(str(arg) for arg in args),
    ]


def child_environment(**overrides) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in _AMBIENT}
    env.update(overrides)
    return env


def error_summary(text: str) -> str | None:
    """Last exception line of child output, e.g. ``ModuleNotFoundError: ...``."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if _ERROR_LINE.match(line):
            return line[:300]
    return None


def _import_pm():
    """Hermes's public package manager, or None when this host lacks it."""
    try:
        import pm
    except ImportError:
        try:
            import hermes_constants
        except ImportError:
            return None
        root = Path(hermes_constants.__file__).resolve().parent
        if not (root / "pm" / "__init__.py").is_file():
            return None
        # A pre-PM editable install maps only the packages it knew about.
        # Append, so nothing already importable is shadowed.
        sys.path.append(str(root))
        try:
            import pm
        except ImportError:
            return None
    try:
        pm.ensure_environment
        pm.environment_python
    except AttributeError:
        return None
    return pm


def _read_marker(root: Path) -> dict:
    try:
        record = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _write_marker(root: Path, record: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".runtime-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        os.replace(name, root / MARKER)
    finally:
        Path(name).unlink(missing_ok=True)


def _selected(root: Path, record: dict) -> Path | None:
    backend = record.get("backend")
    if backend == "pm":
        pm = _import_pm()
        if pm is None:
            return None
        # PM owns selection; read it each time instead of trusting a saved path.
        return pm.environment_python(ENVIRONMENT_NAME, root=root / "pm")
    if backend == "venv" and isinstance(record.get("python"), str):
        python = Path(record["python"])
        return python if python.is_file() else None
    return None


def status(home) -> dict:
    """Read-only runtime state; never starts a process or installs anything."""
    root = environment_root(home)
    record = _read_marker(root)
    if not record:
        return {"state": "missing", "python": None, "root": str(root)}
    try:
        python = _selected(root, record)
    except Exception as exc:  # A damaged PM selection is reported, not raised.
        return {
            "state": "broken",
            "python": None,
            "root": str(root),
            "detail": type(exc).__name__,
        }
    state = "ready"
    if python is None:
        state = "broken"
    elif record.get("requirements") != requirements_digest():
        state = "outdated"
    elif record.get("validated_python") != str(python):
        state = "unvalidated"
    return {
        "state": state,
        "python": str(python) if python else None,
        "root": str(root),
        "backend": record.get("backend"),
        "version": record.get("version"),
    }


def probe(python) -> dict:
    """Import every server module in a real child; nothing is started."""
    try:
        completed = subprocess.run(
            command(python, PROBE_MODULE),
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeUnavailable(
            f"Server runtime could not be run ({type(exc).__name__}); run {REPAIR_COMMAND}"
        ) from exc
    if completed.returncode != 0:
        detail = error_summary(completed.stderr or completed.stdout or "")
        raise RuntimeUnavailable(
            "Server runtime failed its import check"
            + (f" ({detail})" if detail else "")
            + f"; run {REPAIR_COMMAND}"
        )
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeUnavailable(
            f"Server runtime import check returned no report; run {REPAIR_COMMAND}"
        ) from exc


def _validate(root: Path, record: dict, python: Path) -> Path:
    report = probe(python)
    _write_marker(
        root,
        {
            **record,
            "requirements": requirements_digest(),
            "validated_python": str(python),
            "version": report.get("version"),
        },
    )
    return python


def _provision_pm(pm, root: Path) -> tuple[dict, Path]:
    python = pm.ensure_environment(
        ENVIRONMENT_NAME,
        list(SERVER_REQUIREMENTS),
        root=root / "pm",
        explicit=True,
        timeout=PROVISION_TIMEOUT_SECONDS,
    )
    return {"backend": "pm"}, Path(python)


def _provision_venv(root: Path) -> tuple[dict, Path]:
    """Fallback for hosts without PM: a venv from the host's base interpreter."""
    base = Path(getattr(sys, "_base_executable", None) or sys.executable)
    generation = root / "venv" / f"gen-{uuid.uuid4().hex}"
    generation.parent.mkdir(parents=True, exist_ok=True)
    python = generation / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        subprocess.run(
            [str(base), "-I", "-m", "venv", str(generation)],
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=PROVISION_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *SERVER_REQUIREMENTS,
            ],
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=PROVISION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(generation, ignore_errors=True)
        raise RuntimeUnavailable(
            f"Could not build the server runtime ({type(exc).__name__}); "
            "install Hermes's package manager or rerun " + REPAIR_COMMAND
        ) from exc
    return {"backend": "venv", "python": str(python)}, python


def provision(home) -> Path:
    """Explicitly build/select the server runtime and validate it.

    The previous runtime stays selected if anything fails. Only the plugin's
    own environment is written; Hermes's dependencies are never touched.
    """
    from filelock import FileLock

    root = environment_root(home)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".provision.lock"), timeout=PROVISION_TIMEOUT_SECONDS):
        current = status(home)
        if current["state"] == "ready":
            return Path(current["python"])
        pm = _import_pm()
        record, python = _provision_pm(pm, root) if pm else _provision_venv(root)
        return _validate(root, record, python)


def ensure_ready(home, *, allow_provision=False) -> Path:
    """Return a validated server interpreter, provisioning only if allowed."""
    current = status(home)
    if current["state"] == "ready":
        return Path(current["python"])
    if current["state"] == "unvalidated":
        root = environment_root(home)
        try:
            return _validate(root, _read_marker(root), Path(current["python"]))
        except RuntimeUnavailable:
            if not allow_provision:
                raise
    if allow_provision:
        return provision(home)
    raise RuntimeUnavailable(
        f"Claude Native Bridge server runtime is {current['state']}; run {REPAIR_COMMAND}"
    )
