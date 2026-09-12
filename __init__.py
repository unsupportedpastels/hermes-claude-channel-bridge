"""Model-provider discovery entry; generic plugin loading adds no host tools."""


def register(ctx=None):
    # The model-provider loader imports this as a package. Generic inspection
    # may import bare __init__; that surface must not register a second provider.
    if __package__:
        from .claude_native_bridge.provider import register as register_provider

        register_provider()


if __package__:
    register()
