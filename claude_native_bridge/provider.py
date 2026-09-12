"""Filesystem and entry-point registration for the local OpenAI API provider."""

from providers import register_provider
from .api_provider import make_profile

profile = make_profile()


def register():
    register_provider(profile)
