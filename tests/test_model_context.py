"""Provider-qualified context metadata without inference or local overrides."""

from claude_native_bridge.api_provider import make_profile


def test_mythos_context_is_provider_qualified():
    profile = make_profile()
    assert profile.get_model_context_length("claude-mythos-5-1") == 1_000_000
    for model in ("claude-mythos-5-1-preview", "claude-mythos-5", "claude-haiku-4-5-20251001", "unknown"):
        assert profile.get_model_context_length(model) is None


def test_haiku_5_5_context_is_provider_qualified():
    profile = make_profile()
    assert profile.get_model_context_length("claude-haiku-5-5") == 1_000_000
    for model in ("claude-haiku-5", "claude-haiku-5-5-preview", "claude-haiku-4-5-20251001"):
        assert profile.get_model_context_length(model) is None


def test_host_resolves_haiku_5_5_before_stale_cache(monkeypatch):
    import providers
    from agent import model_metadata

    profile = make_profile()
    monkeypatch.setattr(providers, "get_provider_profile", lambda provider: profile)
    monkeypatch.setattr(model_metadata, "_config_override_context_length", lambda *args: None)
    monkeypatch.setattr(model_metadata, "get_cached_context_length", lambda *args, **kwargs: 200_000)
    assert model_metadata.get_model_context_length(
        "claude-haiku-5-5", provider=profile.name, base_url=profile.base_url
    ) == 1_000_000
    assert model_metadata.get_model_context_length(
        "claude-haiku-5-5", config_context_length=128_000,
        provider=profile.name, base_url=profile.base_url,
    ) == 128_000


def test_host_resolves_mythos_before_stale_cache(monkeypatch):
    import providers
    from agent import model_metadata

    profile = make_profile()
    monkeypatch.setattr(providers, "get_provider_profile", lambda provider: profile)
    monkeypatch.setattr(model_metadata, "_config_override_context_length", lambda *args: None)
    monkeypatch.setattr(model_metadata, "get_cached_context_length", lambda *args, **kwargs: 200_000)
    assert model_metadata.get_model_context_length(
        "claude-mythos-5-1", provider=profile.name, base_url=profile.base_url
    ) == 1_000_000
    assert model_metadata.get_model_context_length(
        "claude-mythos-5-1", config_context_length=128_000,
        provider=profile.name, base_url=profile.base_url,
    ) == 128_000
