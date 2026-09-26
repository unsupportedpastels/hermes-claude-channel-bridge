"""Profile-scoped configuration; no credential handling or startup at import."""

import math
from dataclasses import dataclass, fields

# Claude Code's documented default context window when the status line has not
# yet reported one for the running model.
ASSUMED_CONTEXT_WINDOW = 200_000


NATIVE_LOGIN_REFRESH_CONTENTION_TEXT = (
    "Could not refresh your login because another Claude Code process is refreshing it "
    "(or exited mid-refresh) · Try again in a minute; if it keeps happening, close other "
    "Claude Code windows or sign in again with /login"
)
LOGIN_REFRESH_CONTENTION_MESSAGE = (
    "Claude authentication needs attention. Run `claude auth login` as the bridge's "
    "OS user on the machine running the bridge, then start a fresh /review. "
    "Claude credentials may be stale or refresh is blocked. Hermes API keys are not the issue."
)
LOGIN_REFRESH_CONTENTION_CODE = "claude_login_refresh_contention"


class NativeBridgeError(RuntimeError):
    pass


class NativeLoginRefreshContention(NativeBridgeError):
    """Actionable terminal auth failure derived from a recognized native error."""

    def __init__(self):
        super().__init__(LOGIN_REFRESH_CONTENTION_MESSAGE)


class NativeRequestNotDelivered(NativeBridgeError):
    """The native never accepted the request, so no native input is uncertain.

    Raised only for failures before the channel accepted the request window.
    Callers may retry the identical request: the session is retired and a
    rebuild consumes canonical Hermes history, not a stale native state.
    """


@dataclass(frozen=True)
class Settings:
    development_channels_accepted: bool = False
    command: str = "claude"
    effort: str = "medium"
    startup_timeout: float = 30.0
    # Hard cap on one exchange. Frozen or dead CLIs are caught sooner by
    # stall_timeout, so this only bounds a live CLI whose turn never ends.
    request_timeout: float = 1800.0
    # Seconds the native pane may stay unchanged mid-request before the
    # session is treated as frozen. The native spinner repaints every second
    # while it thinks, so an unchanged pane is not a slow model.
    stall_timeout: float = 90.0
    idle_timeout: float = 300.0
    max_sessions: int = 2
    page_threshold: int = 20_000
    bootstrap_max_chars: int = 100_000
    retain_diagnostics: bool = False
    # Channel-side diagnostics are written to the native CLI's own MCP log for
    # this session (ids, sequences and branch labels only; never content).
    channel_diagnostics: bool = False
    # Bridge-driven rotation replaces native automatic compaction: the native
    # session is retired between requests and rebuilt from canonical history.
    native_auto_compact: bool = False
    rotation_percentage: int = 80
    rotation_headroom_tokens: int = 40_000
    rotation_max_tokens: int | None = None
    rotation_fallback_chars: int = 600_000

    @classmethod
    def from_mapping(cls, mapping):
        if not isinstance(mapping, dict):
            raise NativeBridgeError("claude_native_bridge must be a mapping")
        unknown = set(mapping) - {f.name for f in fields(cls)}
        if unknown:
            raise NativeBridgeError(
                "Unknown claude_native_bridge settings: " + ", ".join(sorted(unknown))
            )
        value = cls(**mapping)
        for name in (
            "development_channels_accepted",
            "retain_diagnostics",
            "channel_diagnostics",
            "native_auto_compact",
        ):
            if type(getattr(value, name)) is not bool:
                raise NativeBridgeError(name + " must be boolean")
        for name in (
            "startup_timeout",
            "request_timeout",
            "stall_timeout",
            "idle_timeout",
        ):
            n = getattr(value, name)
            if (
                isinstance(n, bool)
                or not isinstance(n, (int, float))
                or not math.isfinite(n)
                or n <= 0
            ):
                raise NativeBridgeError(name + " must be a finite positive number")
        if type(value.max_sessions) is not int or not 1 <= value.max_sessions <= 10:
            raise NativeBridgeError("max_sessions must be an integer from 1 to 10")
        if type(value.page_threshold) is not int or value.page_threshold <= 0:
            raise NativeBridgeError("page_threshold must be a positive integer")
        if (
            type(value.bootstrap_max_chars) is not int
            or value.bootstrap_max_chars <= 0
        ):
            raise NativeBridgeError("bootstrap_max_chars must be a positive integer")
        if (
            type(value.rotation_percentage) is not int
            or not 1 <= value.rotation_percentage <= 100
        ):
            raise NativeBridgeError(
                "rotation_percentage must be an integer from 1 to 100"
            )
        if (
            type(value.rotation_headroom_tokens) is not int
            or value.rotation_headroom_tokens < 0
        ):
            raise NativeBridgeError(
                "rotation_headroom_tokens must be a non-negative integer"
            )
        if value.rotation_max_tokens is not None and (
            type(value.rotation_max_tokens) is not int
            or value.rotation_max_tokens <= 0
        ):
            raise NativeBridgeError(
                "rotation_max_tokens must be a positive integer or null"
            )
        if (
            type(value.rotation_fallback_chars) is not int
            or value.rotation_fallback_chars <= value.bootstrap_max_chars
        ):
            raise NativeBridgeError(
                "rotation_fallback_chars must be an integer above bootstrap_max_chars"
            )
        if not isinstance(value.command, str) or not value.command.strip():
            raise NativeBridgeError("command must name the installed Claude executable")
        if value.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise NativeBridgeError("Unsupported native effort setting")
        return value

    def check_consent(self):
        if not self.development_channels_accepted:
            raise NativeBridgeError(
                "Set claude_native_bridge.development_channels_accepted: true only after accepting local development-channel use. No Claude process was started."
            )


def rotation_threshold(settings, window):
    """Native context tokens at which the bridge retires and rebuilds a session."""
    if type(window) is not int or window <= 0:
        raise NativeBridgeError("Context window must be a positive integer")
    headroom_limit = window - settings.rotation_headroom_tokens
    if headroom_limit <= 0:
        raise NativeBridgeError(
            "Reported context window does not leave the configured rotation headroom"
        )
    threshold = min(
        window * settings.rotation_percentage // 100,
        headroom_limit,
    )
    if settings.rotation_max_tokens is not None:
        threshold = min(threshold, settings.rotation_max_tokens)
    if threshold <= 0:
        raise NativeBridgeError("Rotation settings leave no usable context capacity")
    return threshold
