"""Pinned native Claude model names; a catalog is not account entitlement.

Sources (verified 2026-09-12, without inference):
https://platform.claude.com/docs/en/about-claude/models/overview
https://platform.claude.com/docs/en/models/opus-4-8/overview
https://code.claude.com/docs/en/model-config

Claude Code accepts the full Anthropic model names. Availability, usage-credit
consent and any organization restrictions remain owned by the native CLI.
"""

MODEL_LABELS = {
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "claude-fable-5-1": "Claude Fable 5.1",
}
MODELS = tuple(MODEL_LABELS)


def reasoning_efforts(model):
    """Haiku 4.5 supports extended thinking, but not the effort parameter."""
    if model == "claude-haiku-4-5-20251001":
        return ()
    return ("low", "medium", "high", "xhigh", "max")
