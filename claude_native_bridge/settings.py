"""Profile-scoped configuration; no credential handling or startup at import."""

from dataclasses import dataclass, fields
import math


class NativeBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    development_channels_accepted: bool = False
    command: str = "claude"
    effort: str = "medium"
    startup_timeout: float = 30.0
    request_timeout: float = 900.0
    idle_timeout: float = 300.0
    max_sessions: int = 2
    page_threshold: int = 20_000
    retain_diagnostics: bool = False

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
        for name in ("development_channels_accepted", "retain_diagnostics"):
            if type(getattr(value, name)) is not bool:
                raise NativeBridgeError(name + " must be boolean")
        for name in ("startup_timeout", "request_timeout", "idle_timeout"):
            n = getattr(value, name)
            if (
                isinstance(n, bool)
                or not isinstance(n, (int, float))
                or not math.isfinite(n)
                or n <= 0
            ):
                raise NativeBridgeError(name + " must be a finite positive number")
        if type(value.max_sessions) is not int or not 1 <= value.max_sessions <= 8:
            raise NativeBridgeError("max_sessions must be an integer from 1 to 8")
        if type(value.page_threshold) is not int or value.page_threshold <= 0:
            raise NativeBridgeError("page_threshold must be a positive integer")
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
