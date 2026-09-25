"""Profile-scoped local API settings. No process starts or writes on import."""

import os
from pathlib import Path

TOKEN_ENV = "CLAUDE_NATIVE_BRIDGE_API_KEY"
DEFAULT_PORT = 18991
DEFAULT_IDLE_EXIT_SECONDS = 300
PROCESS_HEADER = "X-Hermes-Bridge-Process"
_identity: dict[int, str] = {}


def active_home():
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def api_storage(home):
    home = Path(home)
    config = home / "config.yaml"
    # CLI/Desktop can intentionally share a config file. A shared endpoint must
    # have one server/token store, while each caller keeps its own conversation.
    owner = config.resolve().parent if config.exists() else home.resolve()
    return owner / "claude-native-bridge" / "api"


def _api_section(home):
    import yaml

    config = Path(home) / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    return (data or {}).get("claude_native_bridge_api", {})


def configured_port(home):
    value = _api_section(home).get("port", DEFAULT_PORT)
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(
            "claude_native_bridge_api.port must be an integer from 1 to 65535"
        )
    return value


def configured_idle_exit(home):
    """Seconds the service may sit unused with no live client before exiting."""
    value = _api_section(home).get("idle_exit_seconds", DEFAULT_IDLE_EXIT_SECONDS)
    if type(value) is not int or not 0 <= value <= 86400:
        raise ValueError(
            "claude_native_bridge_api.idle_exit_seconds must be an integer from 0 (never) to 86400"
        )
    return value


def api_base_url(home):
    return f"http://127.0.0.1:{configured_port(home)}/v1"


def process_identity():
    """``pid:create_time`` for this process; PID alone can be reused."""
    pid = os.getpid()
    if pid not in _identity:
        import psutil

        _identity.clear()
        _identity[pid] = f"{pid}:{psutil.Process(pid).create_time()!r}"
    return _identity[pid]
