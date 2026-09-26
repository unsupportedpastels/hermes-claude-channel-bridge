"""Bounded, content-free event journal for the bridge API process."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import re


LOGGER = logging.getLogger("claude_native_bridge.events")
LOGGER.propagate = False
EVENTS = frozenset({
    "service_started", "admission_rejected", "generation_started",
    "generation_completed", "generation_failed", "native_failure",
    "service_retiring",
})
REASONS = frozenset({
    "unknown_tool", "text_batch_order", "native_disconnected", "request_timeout",
    "capacity", "replay", "owner_busy", "engine_unavailable", "other",
    "idle", "shutdown_requested", "draining",
})
BRANCHES = frozenset({"not_delivered", "uncertain", "unexpected"})
FIELDS = frozenset({
    "trace", "run", "error_type", "reason", "branch", "stream", "cached",
    "count", "capacity", "tool_count", "duration_ms", "finish",
})
_IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,79}\Z")
_TRACE_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
_RUN = re.compile(r"session-[a-zA-Z0-9_-]{1,64}\Z")
_HANDLER: RotatingFileHandler | None = None
_TRACE: ContextVar[str | None] = ContextVar("bridge_event_trace", default=None)


def configure_event_log(root, *, max_bytes=1_048_576, backup_count=3):
    """One writer (the API process), in its ACL-protected state directory."""
    from .api_service import _private_directory

    if type(max_bytes) is not int or max_bytes < 256:
        raise ValueError("Invalid event log size")
    if type(backup_count) is not int or not 1 <= backup_count <= 10:
        raise ValueError("Invalid event log backup count")
    root = _private_directory(root)
    path = Path(root) / "events.jsonl"
    if path.is_symlink():
        raise ValueError("Event log must not be a symlink")
    close_event_log()
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(path, 0o600)
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    global _HANDLER
    _HANDLER = handler
    return path


def close_event_log():
    global _HANDLER
    if _HANDLER is not None:
        LOGGER.removeHandler(_HANDLER)
        _HANDLER.close()
        _HANDLER = None


def failure_reason(exc):
    """Map exact known failure messages to codes; never persist exception text."""
    message = str(exc)
    if message == "Decision selected a tool absent from the supplied definitions":
        return "unknown_tool"
    if "Missing or out-of-order native text batch" in message:
        return "text_batch_order"
    if isinstance(exc, TimeoutError) or message == "Generation timeout":
        return "request_timeout"
    if "Native generation failed or disconnected" in message:
        return "native_disconnected"
    return "other"


def safe_error_type(exc):
    name = type(exc).__name__
    return name if _IDENTIFIER.fullmatch(name) else "OtherError"


def safe_run(runtime):
    name = Path(runtime).name if runtime is not None else ""
    return name if _RUN.fullmatch(name) else None


@contextmanager
def event_trace(trace):
    if not isinstance(trace, str) or not _TRACE_PATTERN.fullmatch(trace):
        raise ValueError("Invalid trace")
    token = _TRACE.set(trace)
    try:
        yield
    finally:
        _TRACE.reset(token)


def record_event(event, **fields):
    """Reject arbitrary values: no prompts, arguments, credentials or exception text."""
    if event not in EVENTS or fields.keys() - FIELDS:
        raise ValueError("Unsupported bridge event or field")
    if "trace" not in fields and _TRACE.get() is not None:
        fields["trace"] = _TRACE.get()
    for name, value in fields.items():
        if name == "trace" and not (isinstance(value, str) and _TRACE_PATTERN.fullmatch(value)):
            raise ValueError("Invalid trace")
        if name == "run" and not (isinstance(value, str) and _RUN.fullmatch(value)):
            raise ValueError("Invalid run")
        if name == "error_type" and not (isinstance(value, str) and _IDENTIFIER.fullmatch(value)):
            raise ValueError("Invalid error type")
        if name == "reason" and value not in REASONS:
            raise ValueError("Invalid reason")
        if name == "branch" and value not in BRANCHES:
            raise ValueError("Invalid branch")
        if name in ("stream", "cached") and type(value) is not bool:
            raise ValueError("Invalid boolean")
        if name in ("count", "capacity", "tool_count", "duration_ms") and (
            type(value) is not int or not 0 <= value <= 10_000_000
        ):
            raise ValueError("Invalid count")
        if name == "finish" and value not in ("stop", "tool_calls"):
            raise ValueError("Invalid finish reason")
    if _HANDLER is not None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": event,
            **fields,
        }
        LOGGER.info(json.dumps(row, separators=(",", ":"), ensure_ascii=True))
