"""Standard OpenAI HTTP provider registration for the plugin-owned local API."""

import inspect
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

from openai import OpenAI
from providers import register_provider
from providers.base import ProviderProfile

from .api_config import TOKEN_ENV, active_home, api_base_url
from .models import MODELS, reasoning_efforts


class BridgeOpenAI(OpenAI):
    """OpenAI client that releases its plugin-owned native owner on close."""

    def __init__(self, *args, bridge_close_url, bridge_token, bridge_owner, **kwargs):
        self._bridge_close_url = bridge_close_url
        self._bridge_token = bridge_token
        self._bridge_owner = bridge_owner
        self._bridge_owner_closed = False
        super().__init__(*args, **kwargs)

    def close(self):
        if not self._bridge_owner_closed:
            self._bridge_owner_closed = True
            request = Request(
                self._bridge_close_url,
                data=b"",
                method="POST",
                headers={
                    "Authorization": "Bearer " + self._bridge_token,
                    "X-Hermes-Bridge-Client": self._bridge_owner,
                },
            )
            try:
                response = urlopen(request, timeout=2)
                response.close()
            except Exception:
                # Closing the local SDK client must remain safe during process
                # shutdown or when the bridge server has already exited.
                pass
        super().close()


class ClaudeAPIProfile(ProviderProfile):
    def create_client(self, **kwargs):
        from .api_service import ensure_server

        home = active_home()
        base_url = str(kwargs.get("base_url") or api_base_url(home))
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError(
                "Claude bridge requires its configured loopback HTTP API; reselect the provider to migrate an old native URI"
            )
        token = kwargs.get("api_key") or os.environ.get(TOKEN_ENV, "")
        ensure_server(home, token, port=parsed.port or 80)
        supported = set(inspect.signature(OpenAI).parameters)
        arguments = {key: value for key, value in kwargs.items() if key in supported}
        headers = dict(arguments.get("default_headers") or {})
        owner = str(uuid.uuid4())
        headers["X-Hermes-Bridge-Client"] = owner
        arguments.update(api_key=token, base_url=base_url, default_headers=headers)
        return BridgeOpenAI(
            **arguments,
            bridge_close_url=base_url.rstrip("/") + "/owner/close",
            bridge_token=token,
            bridge_owner=owner,
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
        fallback_models=MODELS,
        supports_vision=False,
        supports_vision_tool_messages=False,
    )


def register():
    register_provider(make_profile())
