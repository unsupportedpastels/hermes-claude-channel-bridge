"""Offline, non-invasive readiness diagnostics for the native bridge."""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable, Iterable

from .settings import NativeBridgeError, Settings


# Host-side distribution names and the minimum versions in pyproject.toml.
# There are no upper bounds, so a Hermes update that moves a shared dependency
# forward cannot make the bridge report itself broken. Server-only packages
# live in the isolated server runtime and are checked there, not in Hermes.
PYTHON_DEPENDENCIES = (
    ("PyYAML", (6,)),
    ("httpx", (0, 27)),
    ("openai", (2,)),
    ("filelock", (3, 15)),
    ("python-dotenv", (1,)),
    ("psutil", (5, 9)),
)
WINDOWS_DEPENDENCIES = (
    ("pywin32", (308,)),
)
CHANNEL_DEPENDENCIES = {
    "@modelcontextprotocol/sdk": "1.30.0",
    "zod": "3.25.76",
}

_PLATFORM_DETAILS = {
    "linux": "Linux is supported; native bridge operation has been verified on real Linux hosts.",
    "darwin": "macOS is supported; native bridge operation has been verified on a real Mac.",
    "win32": "Native Windows is supported through ConPTY and has been verified on Windows 11.",
}

# This is evidence, not an allowlist. Other versions remain runnable with a warning.
_VERIFIED_CLI = {
    "linux": {
        "2.1.269": "Verified for direct MCP read_result paging on Linux.",
        "2.1.270": "Verified for ordinary bridge operation on Linux; direct MCP paging has known CLI-sensitive limitations.",
    },
    "darwin": {
        "2.1.270": "Verified for ordinary bridge operation on macOS; direct MCP paging has known CLI-sensitive limitations.",
    },
}
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def _check(check_id: str, status: str, detail: str, **values) -> dict:
    return {"id": check_id, "status": status, **values, "detail": detail}


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = re.match(r"^(\d+(?:\.\d+)*)", version)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _at_least(version: str, minimum: tuple[int, ...]) -> bool:
    parsed = _version_tuple(version)
    if parsed is None:
        return False
    width = max(len(parsed), len(minimum))
    padded = parsed + (0,) * (width - len(parsed))
    lower = minimum + (0,) * (width - len(minimum))
    return padded >= lower


def _current_python_version() -> tuple[int, int, int]:
    return (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )


def _load_settings(home: Path) -> tuple[Settings, dict]:
    config = home / "config.yaml"
    if not config.exists():
        return Settings(), _check(
            "configuration",
            "warn",
            "No config.yaml was found; bridge settings are using safe defaults.",
        )
    try:
        import yaml
    except ImportError:
        return Settings(), _check(
            "configuration",
            "fail",
            "PyYAML is not installed, so config.yaml could not be checked.",
        )
    try:
        document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        if not isinstance(document, dict):
            raise NativeBridgeError("config.yaml must contain a mapping")
        settings = Settings.from_mapping(document.get("claude_native_bridge", {}))
    except (yaml.YAMLError, OSError, UnicodeError, NativeBridgeError, ValueError) as exc:
        return Settings(), _check(
            "configuration",
            "fail",
            f"Bridge configuration is invalid ({type(exc).__name__}); no private values were reported.",
        )
    return settings, _check(
        "configuration", "pass", "Bridge configuration parsed successfully."
    )


def _runtime_check(home: Path) -> dict:
    """Read the server runtime selection without running or installing it."""
    from .runtime_environment import REPAIR_COMMAND, status

    try:
        state = status(home)
    except Exception as exc:
        return _check(
            "server_runtime",
            "warn",
            f"Server runtime state could not be read ({type(exc).__name__}).",
            state="unknown",
        )
    if state["state"] == "ready":
        return _check(
            "server_runtime",
            "pass",
            "The isolated server runtime is prepared and validated.",
            state="ready",
            version=state.get("version"),
        )
    return _check(
        "server_runtime",
        "fail" if state["state"] == "broken" else "warn",
        f"The isolated server runtime is {state['state']}; run {REPAIR_COMMAND}. "
        "With development-channel consent recorded, first use prepares it.",
        state=state["state"],
    )


def _dependency_contracts(
    platform_name: str, required_distributions: Iterable[str] | None
) -> tuple[tuple[str, tuple[int, ...] | None], ...]:
    if required_distributions is not None:
        return tuple((name, None) for name in required_distributions)
    contracts = PYTHON_DEPENDENCIES
    if platform_name == "win32":
        contracts += WINDOWS_DEPENDENCIES
    return contracts


def _executable_names(platform_name: str, claude_command: str) -> tuple[str, ...]:
    prerequisites = (claude_command, "node")
    if platform_name in {"linux", "darwin"}:
        prerequisites += ("tmux",)
    return prerequisites


def _channel_dependency_checks(channel_root: Path) -> list[dict]:
    checks = []
    for name, expected in CHANNEL_DEPENDENCIES.items():
        manifest = (
            channel_root
            / "node_modules"
            / Path(*name.split("/"))
            / "package.json"
        )
        installed = None
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(document, dict) and isinstance(document.get("version"), str):
                installed = document["version"]
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        checks.append(
            _check(
                f"dependency:{name}",
                "pass" if installed == expected else "fail",
                "Installed channel dependency matches the pinned package contract."
                if installed == expected
                else "Pinned channel dependency is missing or has the wrong version; run npm ci for the channel package.",
                version=installed,
            )
        )
    return checks


def doctor(
    home: str | Path,
    *,
    check_cli_version: bool = False,
    executable_resolver: Callable[[str], str | None] = shutil.which,
    distribution_version: Callable[[str], str] = metadata.version,
    required_distributions: Iterable[str] | None = None,
    platform_name: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    channel_root: str | Path | None = None,
) -> dict:
    """Return safe local checks without starting services or accessing auth state.

    The only process invocation is ``claude --version``, and it occurs solely
    when ``check_cli_version`` is true. Only a parsed numeric version is kept.
    """

    platform_name = sys.platform if platform_name is None else platform_name
    settings, config_check = _load_settings(Path(home))
    checks = [config_check]

    platform_detail = _PLATFORM_DETAILS.get(platform_name)
    checks.append(
        _check(
            "platform",
            "pass" if platform_detail else "warn",
            platform_detail
            or "This platform has no recorded bridge verification; compatibility is unknown.",
            platform=platform_name,
            compatibility="known" if platform_detail else "unknown",
        )
    )

    checks.append(
        _check(
            "python",
            "pass",
            "The bridge declares no Python version; Hermes selects the interpreter.",
            version=".".join(str(part) for part in _current_python_version()),
        )
    )
    checks.append(_runtime_check(Path(home)))

    resolved: dict[str, str | None] = {}
    for name in _executable_names(platform_name, settings.command):
        path = executable_resolver(name)
        resolved[name] = path
        label = "claude" if name == settings.command else name
        checks.append(
            _check(
                f"executable:{label}",
                "pass" if path else "fail",
                "Executable resolved locally." if path else "Required executable was not found on PATH or at the configured path.",
                resolved_path=path,
            )
        )

    checks.append(
        _check(
            "node_runtime_version",
            "warn",
            "Node 22 or newer is required, but its version was not invoked by this offline doctor.",
            version=None,
            compatibility="unknown",
        )
    )

    for name, minimum in _dependency_contracts(
        platform_name, required_distributions
    ):
        try:
            installed = distribution_version(name)
        except (metadata.PackageNotFoundError, LookupError):
            checks.append(
                _check(
                    f"dependency:{name}",
                    "fail",
                    "Required Python distribution is not installed.",
                    version=None,
                )
            )
            continue
        compatible = minimum is None or _at_least(installed, minimum)
        checks.append(
            _check(
                f"dependency:{name}",
                "pass" if compatible else "fail",
                "Installed Python distribution meets the declared minimum version."
                if compatible and minimum is not None
                else "Installed Python distribution was found."
                if compatible
                else "Installed version is older than the declared minimum.",
                version=installed,
            )
        )

    channel_root = (
        Path(__file__).parent / "channel"
        if channel_root is None
        else Path(channel_root)
    )
    checks.extend(_channel_dependency_checks(channel_root))

    accepted = settings.development_channels_accepted
    checks.append(
        _check(
            "development_channel_consent",
            "pass" if accepted else "fail",
            "Explicit local development-channel consent is recorded."
            if accepted
            else "Explicit development-channel consent is required before live use.",
            accepted=accepted,
        )
    )

    claude_path = resolved.get(settings.command)
    version_check = _check(
        "claude_cli_version",
        "warn",
        "Claude CLI version was not invoked; compatibility is unknown. Use --check-cli-version for the local version probe.",
        version=None,
        compatibility="unknown",
    )
    version_probe_performed = False
    if check_cli_version:
        if not claude_path:
            version_check = _check(
                "claude_cli_version",
                "warn",
                "Claude CLI version could not be checked because the executable was not resolved.",
                version=None,
                compatibility="unknown",
            )
        else:
            try:
                version_probe_performed = True
                completed = runner(
                    [claude_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                match = _VERSION_RE.search(completed.stdout or "")
            except (OSError, subprocess.SubprocessError):
                match = None
            version = match.group(1) if match else None
            detail = _VERIFIED_CLI.get(platform_name, {}).get(version or "")
            version_check = _check(
                "claude_cli_version",
                "pass" if detail else "warn",
                detail
                or "The local Claude CLI version is unverified for this platform; this is a warning, not a version pin.",
                version=version,
                compatibility="known" if detail else "unknown",
            )
    checks.append(version_check)

    counts = {
        state: sum(check["status"] == state for check in checks)
        for state in ("pass", "warn", "fail")
    }
    return {
        "schema_version": 1,
        "offline": True,
        "version_probe_requested": check_cli_version,
        "version_probe_performed": version_probe_performed,
        "ready": counts["fail"] == 0,
        "summary": counts,
        "checks": checks,
    }
