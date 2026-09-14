"""Standard OpenAI HTTP provider registration for the plugin-owned local API."""

import inspect
import os
import threading
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml
from openai import OpenAI
from providers import register_provider
from providers.base import ProviderProfile

from .api_config import TOKEN_ENV, active_home, api_base_url, api_storage
from .models import MODELS, reasoning_efforts

OWNER_HEADER = "X-Hermes-Bridge-Client"
RETRY_LINEAGE_HEADER = "X-Hermes-Bridge-Retry-Lineage"
LEASES_HEADER = "X-Hermes-Bridge-Leases"
_UNBOUND_RETRY_LINEAGE = "unbound"
_lease_lock = threading.Lock()
_lineage_leases: dict[str, set[str]] = {}


def _retry_lineage(headers):
    """Bind a lineage to Hermes' durable parent client-kwargs header mapping."""
    lineage = headers.get(RETRY_LINEAGE_HEADER) if headers is not None else None
    try:
        lineage = str(uuid.UUID(lineage))
    except (AttributeError, TypeError, ValueError):
        lineage = str(uuid.uuid4())
        if isinstance(headers, MutableMapping):
            headers[RETRY_LINEAGE_HEADER] = lineage
    return lineage


def _register_lease(lineage, lease):
    with _lease_lock:
        _lineage_leases.setdefault(lineage, set()).add(lease)


def _unregister_lease(lineage, lease):
    with _lease_lock:
        leases = _lineage_leases.get(lineage)
        if leases is None:
            return
        leases.discard(lease)
        if not leases:
            _lineage_leases.pop(lineage, None)


def _active_leases(lineage):
    with _lease_lock:
        return tuple(sorted(_lineage_leases.get(lineage, ())))


class BridgeOpenAI(OpenAI):
    """OpenAI client that releases its plugin-owned native owner on close."""

    def __init__(
        self,
        *args,
        bridge_close_url,
        bridge_token,
        bridge_owner,
        bridge_lineage=None,
        **kwargs,
    ):
        self._bridge_close_url = bridge_close_url
        self._bridge_token = bridge_token
        self._bridge_owner = bridge_owner
        self._bridge_lineage = bridge_lineage or str(uuid.uuid4())
        self._bridge_owner_closed = False
        _register_lease(self._bridge_lineage, bridge_owner)
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            _unregister_lease(self._bridge_lineage, bridge_owner)
            raise

    @property
    def default_headers(self):
        # The SDK evaluates this property for every request. Publishing all
        # sibling leases lets the service retain a logical owner before an
        # otherwise-idle shared primary client makes its summary request.
        headers = dict(super().default_headers)
        headers[LEASES_HEADER] = ",".join(_active_leases(self._bridge_lineage))
        return headers

    def close(self):
        if not self._bridge_owner_closed:
            self._bridge_owner_closed = True
            request = Request(
                self._bridge_close_url,
                data=b"",
                method="POST",
                headers={
                    "Authorization": "Bearer " + self._bridge_token,
                    OWNER_HEADER: self._bridge_owner,
                    RETRY_LINEAGE_HEADER: self._bridge_lineage,
                },
            )
            try:
                response = urlopen(request, timeout=2)
                response.close()
            except Exception:
                # Closing the local SDK client must remain safe during process
                # shutdown or when the bridge server has already exited.
                pass
            finally:
                _unregister_lease(self._bridge_lineage, self._bridge_owner)
        super().close()


CONSENT_KEY = "development_channels_accepted"


def _consent_recorded(home):
    """True when config.yaml already records development-channel consent."""
    config = Path(home) / "config.yaml"
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    section = (data or {}).get("claude_native_bridge")
    return isinstance(section, dict) and section.get(CONSENT_KEY) is True


def _first_run(home, token):
    """Return a usable ``(token, base_url)`` or fail with a setup pointer.

    Hermes' plugin installer never runs npm or plugin setup, so a freshly
    installed plugin can be selected before it is usable. When config.yaml
    already records development-channel consent, the missing steps run here
    on first use; otherwise the explicit setup command is required, because
    that consent must come from a person.
    """
    from . import channel_install

    ready = channel_install.channel_dependencies_ready()
    if token and ready:
        return token, None
    if not _consent_recorded(home):
        problem = (
            "channel dependencies are not installed"
            if token
            else "has no local API credential"
        )
        raise ValueError(
            "Claude Native Bridge "
            + problem
            + "; run "
            + channel_install.SETUP_COMMAND
            + " and restart Hermes, or set claude_native_bridge."
            + CONSENT_KEY
            + ": true in config.yaml to let the first use run setup"
        )
    if not ready:
        channel_install.install_channel_dependencies()
    if token:
        return token, None
    from .api_service import setup

    info = setup(home, accept_development_channels=True)
    keyfile = api_storage(home) / "token"
    return keyfile.read_text(encoding="utf-8").strip(), info["base_url"]


class ClaudeAPIProfile(ProviderProfile):
    def create_client(self, **kwargs):
        from .api_service import ensure_server

        configured = kwargs.get("base_url")
        home = None
        if configured:
            base_url = str(configured)
        else:
            home = active_home()
            base_url = api_base_url(home)
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError(
                "Claude bridge requires its configured loopback HTTP API; reselect the provider to migrate an old native URI"
            )
        if home is None:
            home = active_home()
        token = kwargs.get("api_key") or os.environ.get(TOKEN_ENV, "")
        token, configured_url = _first_run(home, token)
        if configured_url:
            # First-use setup chose the port; the profile's import-time URL
            # predates it.
            base_url = configured_url
            parsed = urlsplit(base_url)
        ensure_server(home, token, port=parsed.port or 80)
        supported = set(inspect.signature(OpenAI).parameters)
        arguments = {key: value for key, value in kwargs.items() if key in supported}
        inherited_headers = arguments.get("default_headers")
        headers = dict(inherited_headers or {})
        headers[RETRY_LINEAGE_HEADER] = _retry_lineage(inherited_headers)
        owner = str(uuid.uuid4())
        headers[OWNER_HEADER] = owner
        arguments.update(api_key=token, base_url=base_url, default_headers=headers)
        return BridgeOpenAI(
            **arguments,
            bridge_close_url=base_url.rstrip("/") + "/owner/close",
            bridge_token=token,
            bridge_owner=owner,
            bridge_lineage=headers[RETRY_LINEAGE_HEADER],
        )

    def build_extra_body(self, *, session_id=None, **context):
        return {"hermes_session_id": session_id} if session_id else {}

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        effort = (reasoning_config or {}).get("effort")
        return ({}, {"reasoning_effort": effort}) if effort else ({}, {})

    def supported_reasoning_efforts(self, model):
        return reasoning_efforts(model)


def make_profile(home=None):
    return ClaudeAPIProfile(
        name="claude-native-bridge",
        display_name="Claude Native Bridge",
        description="Plugin-owned OpenAI-compatible API backed by native Claude Code.",
        auth_type="api_key",
        api_mode="chat_completions",
        env_vars=(TOKEN_ENV,),
        base_url=api_base_url(home or active_home()),
        # Hermes copies this mapping into each AIAgent's stored client kwargs.
        # create_client replaces the marker in that per-agent mapping, so later
        # request-client rebuilds inherit it while independent agents do not.
        default_headers={RETRY_LINEAGE_HEADER: _UNBOUND_RETRY_LINEAGE},
        fallback_models=MODELS,
        supports_vision=False,
        supports_vision_tool_messages=False,
    )


def register():
    register_provider(make_profile())
