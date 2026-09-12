"""Standard OpenAI HTTP provider registration for the plugin-owned local API."""

import inspect
import os
from urllib.parse import urlsplit
import uuid

from providers import register_provider
from providers.base import ProviderProfile

from .api_config import TOKEN_ENV, active_home, api_base_url
from .models import MODELS, reasoning_efforts


class ClaudeAPIProfile(ProviderProfile):
    def create_client(self, **kwargs):
        from openai import OpenAI
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
        headers["X-Hermes-Bridge-Client"] = str(uuid.uuid4())
        arguments.update(api_key=token, base_url=base_url, default_headers=headers)
        return OpenAI(**arguments)

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
