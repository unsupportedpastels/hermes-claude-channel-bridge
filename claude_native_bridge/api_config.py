"""Profile-scoped local API settings. No process starts or writes on import."""

from pathlib import Path
import yaml

TOKEN_ENV = "CLAUDE_NATIVE_BRIDGE_API_KEY"
DEFAULT_PORT = 18991


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


def configured_port(home):
    config = Path(home) / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    section = (data or {}).get("claude_native_bridge_api", {})
    value = section.get("port", DEFAULT_PORT)
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(
            "claude_native_bridge_api.port must be an integer from 1 to 65535"
        )
    return value


def api_base_url(home):
    return f"http://127.0.0.1:{configured_port(home)}/v1"
