"""Authenticated local OpenAI transport; native engines never execute tools here.

The injected factory has the same keyword interface as NativeBridgeClient.
Limits belong to this service, not to native inference configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .event_log import event_trace, failure_reason, record_event, safe_error_type
from .models import MODELS
from .protocol import _choice, _messages, _tool_definitions, _validate_arguments
from .settings import (
    LOGIN_REFRESH_CONTENTION_CODE,
    LOGIN_REFRESH_CONTENTION_MESSAGE,
    NativeLoginRefreshContention,
    NativeRequestNotDelivered,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_OWNERS = 32
OWNER_IDLE_SECONDS = 600.0
REQUEST_TIMEOUT_SECONDS = 600.0
CLOSE_TIMEOUT_SECONDS = 5.0
_PROVENANCE_FIELD = "native_bridge_usage_provenance"
_PROVENANCE_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_PROVENANCE_CORRELATIONS = frozenset(
    {"correlated", "missing", "mismatch", "ambiguous"}
)
_COMPACTION_FIELD = "native_bridge_compaction"
_COMPACTION_STATUSES = frozenset({"compacting", "completed", "failed"})
_COMPACTION_TRIGGERS = frozenset({"auto", "manual"})
_COMPACTION_ERRORS = frozenset(
    {
        "invalid_event",
        "overlapping_start",
        "invalid_context",
        "correlation_mismatch",
        "limit_exceeded",
        "missing_start",
        "missing_summary",
        "missing_end",
    }
)
_MAX_COMPACTION_GENERATION = 16_384
_MAX_COMPACTION_ID_LENGTH = 4096
_MAX_COMPACTION_SUMMARY_BYTES = 8 * 1024 * 1024
_ROTATION_FIELD = "native_bridge_rotation"
_UNEXPECTED_COMPACTION_FIELD = "native_bridge_unexpected_compaction"
_ROTATION_REASONS = frozenset(
    {"context_tokens", "incoming_admission", "uncorrelated_usage_chars"}
)
_ROTATION_WINDOW_SOURCES = frozenset({"native_status_line", "assumed"})
_MAX_DIAGNOSTIC_COUNTER = (1 << 63) - 1
MAX_TOMBSTONES = 1024
TOMBSTONE_SECONDS = OWNER_IDLE_SECONDS
_ALLOWED = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "response_format",
        "hermes_session_id",
        "reasoning_effort",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "seed",
        "n",
        "parallel_tool_calls",
        "presence_penalty",
        "frequency_penalty",
        "user",
    }
)


def _canonical_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _plain(value):
    if isinstance(value, SimpleNamespace):
        return {k: _plain(v) for k, v in vars(value).items()}
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _safe_usage_provenance(value, selected_model):
    """Project only bounded native status evidence onto the public API."""
    candidate = (
        value.get(_PROVENANCE_FIELD)
        if isinstance(value, dict)
        else getattr(value, _PROVENANCE_FIELD, None)
    )
    if not isinstance(candidate, dict):
        return None
    correlation = candidate.get("correlation_status")
    observed = candidate.get("observed_model")
    if (
        candidate.get("source") != "native_status_line"
        or candidate.get("selected_model") != selected_model
        or correlation not in _PROVENANCE_CORRELATIONS
        or (observed is not None and observed not in MODELS)
        or (correlation == "missing" and observed is not None)
    ):
        return None

    counters = {name: None for name in _PROVENANCE_COUNTERS}
    if correlation == "correlated":
        raw = candidate.get("raw_counters")
        if (
            observed != selected_model
            or not isinstance(raw, dict)
            or any(type(raw.get(name)) is not int or raw[name] < 0 for name in counters)
        ):
            return None
        counters = {name: raw[name] for name in _PROVENANCE_COUNTERS}

    return {
        "source": "native_status_line",
        "correlation_status": correlation,
        "selected_model": selected_model,
        "observed_model": observed,
        "raw_counters": counters,
    }


def _safe_compaction(value):
    """Project only bounded lifecycle fields; never serialize native hook payloads."""
    candidate = (
        value.get(_COMPACTION_FIELD)
        if isinstance(value, dict)
        else getattr(value, _COMPACTION_FIELD, None)
    )
    if not isinstance(candidate, dict):
        return None
    status = candidate.get("status")
    trigger = candidate.get("trigger")
    request_id = candidate.get("request_id")
    active = candidate.get("active_request")
    generation = candidate.get("generation")
    summary_bytes = candidate.get("summary_bytes")
    error = candidate.get("error")
    if (
        status not in _COMPACTION_STATUSES
        or trigger not in _COMPACTION_TRIGGERS
        or type(active) is not bool
        or type(generation) is not int
        or not 1 <= generation <= _MAX_COMPACTION_GENERATION
        or not (
            request_id is None
            or (
                isinstance(request_id, str)
                and 0 < len(request_id) <= _MAX_COMPACTION_ID_LENGTH
            )
        )
        or (active and request_id is None)
        or (not active and request_id is not None)
    ):
        return None
    if status == "completed":
        if (
            type(summary_bytes) is not int
            or not 0 <= summary_bytes <= _MAX_COMPACTION_SUMMARY_BYTES
            or error is not None
        ):
            return None
    elif status == "failed":
        if summary_bytes is not None or error not in _COMPACTION_ERRORS:
            return None
    elif status == "compacting" and (
        summary_bytes is not None or error not in (None, "missing_end")
    ):
        return None
    return {
        "status": status,
        "trigger": trigger,
        "request_id": request_id,
        "active_request": active,
        "generation": generation,
        "summary_bytes": summary_bytes,
        "error": error,
    }


def _safe_rotation(value):
    """Project bounded rotation evidence without promoting estimates to tokens."""
    candidate = (
        value.get(_ROTATION_FIELD)
        if isinstance(value, dict)
        else getattr(value, _ROTATION_FIELD, None)
    )
    if not isinstance(candidate, dict) or candidate.get("rotated") is not True:
        return None
    reason = candidate.get("reason")
    exchanges = candidate.get("native_exchanges")
    if (
        reason not in _ROTATION_REASONS
        or type(exchanges) is not int
        or not 1 <= exchanges <= _MAX_DIAGNOSTIC_COUNTER
    ):
        return None
    if reason in {"context_tokens", "incoming_admission"}:
        observed = candidate.get("observed_tokens")
        threshold = candidate.get("threshold_tokens")
        window = candidate.get("window_tokens")
        telemetry_exchange = candidate.get("telemetry_exchange")
        if (
            type(observed) is not int
            or type(threshold) is not int
            or type(window) is not int
            or type(telemetry_exchange) is not int
            or not 0 <= observed <= _MAX_DIAGNOSTIC_COUNTER
            or not 1 <= threshold <= _MAX_DIAGNOSTIC_COUNTER
            or not 1 <= window <= _MAX_DIAGNOSTIC_COUNTER
            or threshold >= window
            or telemetry_exchange != exchanges
            or candidate.get("window_source") not in _ROTATION_WINDOW_SOURCES
        ):
            return None
        projected = {
            "rotated": True,
            "reason": reason,
            "observed_tokens": observed,
            "threshold_tokens": threshold,
            "window_tokens": window,
            "window_source": candidate["window_source"],
            "native_exchanges": exchanges,
            "telemetry_exchange": telemetry_exchange,
        }
        if reason == "context_tokens":
            return projected if observed >= threshold else None

        estimate = candidate.get("incoming_estimate")
        if (
            observed >= threshold
            or not isinstance(estimate, dict)
            or type(estimate.get("bytes")) is not int
            or not 1 <= estimate["bytes"] <= _MAX_DIAGNOSTIC_COUNTER
            or estimate["bytes"] <= threshold - observed
            or estimate.get("source") != "utf8_bytes_conservative_bound"
            or "native_tokens" not in estimate
            or estimate["native_tokens"] is not None
            or type(estimate.get("saturated")) is not bool
        ):
            return None
        projected["incoming_estimate"] = {
            "bytes": estimate["bytes"],
            "source": "utf8_bytes_conservative_bound",
            "native_tokens": None,
            "saturated": estimate["saturated"],
        }
        return projected

    observed = candidate.get("observed_chars")
    threshold = candidate.get("threshold_chars")
    estimate = candidate.get("incoming_estimate")
    if (
        type(observed) is not int
        or type(threshold) is not int
        or not 0 <= observed <= _MAX_DIAGNOSTIC_COUNTER
        or not 1 <= threshold <= _MAX_DIAGNOSTIC_COUNTER
        or not isinstance(estimate, dict)
        or type(estimate.get("chars")) is not int
        or not 0 <= estimate["chars"] <= _MAX_DIAGNOSTIC_COUNTER
        or estimate.get("source") != "serialized_frame_chars"
        or "native_tokens" not in estimate
        or estimate["native_tokens"] is not None
        or type(estimate.get("saturated")) is not bool
        or (observed < threshold and estimate["chars"] <= threshold - observed)
    ):
        return None
    return {
        "rotated": True,
        "reason": reason,
        "observed_chars": observed,
        "threshold_chars": threshold,
        "native_exchanges": exchanges,
        "incoming_estimate": {
            "chars": estimate["chars"],
            "source": "serialized_frame_chars",
            "native_tokens": None,
            "saturated": estimate["saturated"],
        },
    }


def _safe_unexpected_compaction(value):
    candidate = (
        value.get(_UNEXPECTED_COMPACTION_FIELD)
        if isinstance(value, dict)
        else getattr(value, _UNEXPECTED_COMPACTION_FIELD, None)
    )
    return True if candidate is True else None


def _validate(body):
    if not isinstance(body, dict) or set(body) - _ALLOWED:
        raise ValueError("Unsupported request fields")
    if not isinstance(body.get("model"), str) or body["model"] not in MODELS:
        raise ValueError("Unsupported model")
    if not body.get("messages"):
        raise ValueError("messages must be nonempty")
    _messages(body["messages"])
    _choice(body.get("tool_choice"), _tool_definitions(body.get("tools")))
    for name in ("stream", "parallel_tool_calls"):
        if name in body and type(body[name]) is not bool:
            raise ValueError("Expected boolean")
    response_format = body.get("response_format")
    if response_format is not None:
        if (
            not isinstance(response_format, dict)
            or response_format.get("type") not in ("json_object", "json_schema")
            or len(json.dumps(response_format, allow_nan=False).encode()) > 65_536
        ):
            raise ValueError("Unsupported response_format")
    for name in ("hermes_session_id", "user", "reasoning_effort"):
        if name in body and (not isinstance(body[name], str) or len(body[name]) > 512):
            raise ValueError("Expected bounded string")
    if body.get("reasoning_effort") not in (
        None,
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ):
        raise ValueError("Unsupported effort")
    for name in ("max_tokens", "max_completion_tokens", "seed", "n"):
        if name in body and type(body[name]) is not int:
            raise ValueError("Expected integer")
    for name in ("max_tokens", "max_completion_tokens"):
        if name in body and body[name] <= 0:
            raise ValueError("Expected positive token limit")
    if body.get("n", 1) != 1:
        raise ValueError("Only n=1 is supported")
    for name in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if name in body and type(body[name]) not in (int, float):
            raise ValueError("Expected number")
    stop = body.get("stop")
    if (
        stop is not None
        and not isinstance(stop, str)
        and not (isinstance(stop, list) and all(isinstance(x, str) for x in stop))
    ):
        raise ValueError("Invalid stop")
    options = body.get("stream_options")
    if options is not None and (
        not isinstance(options, dict)
        or set(options) - {"include_usage"}
        or type(options.get("include_usage", False)) is not bool
    ):
        raise ValueError("Invalid stream options")
    json.dumps(body, allow_nan=False)


def _completion(value, body):
    provenance = _safe_usage_provenance(value, body["model"])
    compaction = _safe_compaction(value)
    rotation = _safe_rotation(value)
    unexpected_compaction = _safe_unexpected_compaction(value)
    value = _plain(value)
    choices = value["choices"]
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Invalid choices")
    choice = choices[0]
    message = choice["message"]
    content = message.get("content")
    calls = message.get("tool_calls")
    finish = choice.get("finish_reason")
    if message.get("role") != "assistant" or (
        content is not None and not isinstance(content, str)
    ):
        raise ValueError("Invalid message")
    definitions = _tool_definitions(body.get("tools"))
    mode, specified = _choice(body.get("tool_choice"), definitions)
    converted = []
    if calls:
        if (
            finish != "tool_calls"
            or mode == "none"
            or not isinstance(calls, list)
            or len(calls) > 16
        ):
            raise ValueError("Invalid tool completion")
        ids = set()
        for call in calls:
            func = call["function"]
            name, arguments = func["name"], func["arguments"]
            if (
                call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or call["id"] in ids
                or name not in definitions
                or (specified is not None and name != specified)
                or not isinstance(arguments, str)
            ):
                raise ValueError("Invalid tool call")
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            _validate_arguments(args, definitions[name].get("parameters", {}))
            ids.add(call["id"])
            converted.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
    elif finish != "stop" or content is None or mode == "required":
        # Length truncation, StopFailure and missing final authority are not success.
        raise ValueError("Incomplete completion")
    result = {
        "id": value.get("id") or "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": value.get("created", int(time.time())),
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": converted or None,
                },
                "finish_reason": finish,
            }
        ],
        "usage": value.get("usage"),
    }
    if provenance is not None:
        result[_PROVENANCE_FIELD] = provenance
    if compaction is not None:
        result[_COMPACTION_FIELD] = compaction
    if rotation is not None:
        result[_ROTATION_FIELD] = rotation
    if unexpected_compaction is not None:
        result[_UNEXPECTED_COMPACTION_FIELD] = unexpected_compaction
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_BODY_BYTES:
        raise ValueError("Native result exceeds bounded cache")
    return result


def _not_delivered(exc):
    """True only when the failure proves the native never accepted the request.

    Deliberately narrow: an engine reports this only for failures raised before
    it called the channel, so the tombstone keeps covering every case where a
    native turn may already exist.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, NativeRequestNotDelivered):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _terminal_error(exc):
    """Return a fixed safe provider error for the one recognized terminal failure."""
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, NativeLoginRefreshContention):
            return {
                "message": LOGIN_REFRESH_CONTENTION_MESSAGE,
                "type": "authentication_error",
                "code": LOGIN_REFRESH_CONTENTION_CODE,
            }
        seen.add(id(current))
        current = current.__cause__
    return None


def _error_response(error):
    return JSONResponse({"error": error}, status_code=502)


class _TerminalHTTPError(Exception):
    def __init__(self, error):
        super().__init__(error["code"])
        self.error = error


def _log_generation_failure(exc, uncertain, *, trace, stream, started):
    """Bounded failure evidence: class names and our own branch label only."""
    logger.warning(
        "bridge generation %s: %s",
        "uncertain" if uncertain else "not_delivered",
        type(exc).__name__,
    )
    record_event(
        "generation_failed",
        trace=trace,
        stream=stream,
        branch="uncertain" if uncertain else "not_delivered",
        error_type=safe_error_type(exc),
        reason=failure_reason(exc),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@dataclass
class Owner:
    engine: object
    ephemeral: bool = False
    busy: bool = False
    touched: float = field(default_factory=time.monotonic)
    fingerprint: str | None = None
    result: dict | None = None
    failed: bool = False
    terminal_error: dict | None = None
    # The last failure proved the native never accepted the request, so an
    # identical retry is admitted instead of being treated as a replay.
    replayable: bool = False
    task: asyncio.Task | None = None
    retirement_task: asyncio.Task | None = None
    release_task: asyncio.Task | None = None
    close_task: asyncio.Task | None = None
    close_confirmed: bool = False
    capacity_released: bool = False
    removal_requested: bool = False
    cleanup_started: bool = False
    cleanup_completed: bool = False
    cleanup_success: bool | None = None
    cleanup_deadline: float | None = None
    retry_key: tuple[str, str, str] | None = None
    lineage: str | None = None
    lease_refs: set[str] = field(default_factory=set)


def _cleanup_proof(engine):
    """Return (explicit seam present, physical capacity is proven free)."""
    explicit = False
    for name in ("cleanup_confirmed", "cleanup_resource_free"):
        try:
            marker = getattr(engine, name)
        except AttributeError:
            continue
        except BaseException:
            explicit = True
            continue
        explicit = True
        if marker is True:
            return True, True
    return explicit, False


def _resource_free_cleanup(engine):
    """Only an explicit capability may make a hung close capacity-safe."""
    _, confirmed = _cleanup_proof(engine)
    return confirmed


async def _run_close(owner, engine):
    # Cancellation must not queue behind a saturated inference executor.
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def resolve(returned):
        if not done.done():
            done.set_result(returned)

    def close():
        returned = False
        try:
            result = engine.close()
            if inspect.isawaitable(result):
                asyncio.run(result)
            returned = True
        except BaseException:
            pass  # Never leak engine diagnostics/prompts into server logs.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(resolve, returned)

    threading.Thread(target=close, daemon=True, name="bridge-close").start()
    while not done.done():
        _, confirmed = _cleanup_proof(engine)
        if confirmed:
            owner.capacity_released = True
        try:
            await asyncio.wait_for(asyncio.shield(done), 0.1)
        except TimeoutError:
            pass
    returned = await done
    while True:
        explicit, confirmed = _cleanup_proof(engine)
        if confirmed or (returned and not explicit):
            owner.close_confirmed = True
            if owner.engine is engine:
                owner.engine = None
            owner.capacity_released = True
            return True
        if not explicit:
            return False
        # An explicit native outcome can become authoritative after close reports
        # an aggregated error (for example, a late process-exit observation).
        await asyncio.sleep(0.1)


async def _join_cleanup(task, allowance):
    """Shield retained cleanup through repeated cancellation, but never forever."""
    deadline = asyncio.get_running_loop().time() + allowance
    cancelled = False
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.CancelledError:
            cancelled = True
            continue
        except TimeoutError:
            break
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
    if cancelled:
        raise asyncio.CancelledError
    return task.done()


async def _close(owner, *, bounded=True):
    engine = owner.engine
    if engine is None:
        owner.close_confirmed = True
        owner.capacity_released = True
        return True
    if owner.close_task is None:
        owner.close_task = asyncio.create_task(_run_close(owner, engine))
    if bounded:
        completed = await _join_cleanup(owner.close_task, CLOSE_TIMEOUT_SECONDS)
        if not completed and _resource_free_cleanup(engine):
            owner.capacity_released = True
        return owner.close_confirmed
    return await asyncio.shield(owner.close_task)


class Owners:
    def __init__(self, factory, home, limit=None):
        self.factory, self.home = factory, home
        self.limit = MAX_OWNERS if limit is None else limit
        self.items = {}
        self.tombstones: dict[tuple[str, str, str], float] = {}
        self.terminal_failures: dict[tuple[str, str, str], dict] = {}

    def _prune_tombstones(self):
        expired = time.monotonic() - TOMBSTONE_SECONDS
        self.tombstones = {
            key: created for key, created in self.tombstones.items() if created >= expired
        }
        self.terminal_failures = {
            key: error
            for key, error in self.terminal_failures.items()
            if key in self.tombstones
        }

    def _remember_failure(self, owner):
        if owner.retry_key is None:
            return
        self._prune_tombstones()
        if len(self.tombstones) >= MAX_TOMBSTONES:
            oldest = min(self.tombstones, key=lambda item: self.tombstones[item])
            self.tombstones.pop(oldest, None)
            self.terminal_failures.pop(oldest, None)
        self.tombstones[owner.retry_key] = time.monotonic()
        if owner.terminal_error is not None:
            self.terminal_failures[owner.retry_key] = dict(owner.terminal_error)

    def _capacity_used(self):
        return sum(not owner.capacity_released for owner in self.items.values())

    def _start_release(self, key, owner):
        owner.removal_requested = True
        owner.busy = True
        if owner.release_task is None:
            owner.release_task = asyncio.create_task(self._release_owner(key, owner))
        return owner.release_task

    async def _bounded_release(self, owner):
        assert owner.release_task is not None
        completed = await _join_cleanup(
            owner.release_task, CLOSE_TIMEOUT_SECONDS + 0.1
        )
        if not completed and owner.engine is not None:
            if _resource_free_cleanup(owner.engine):
                owner.capacity_released = True

    async def prune(self):
        self._prune_tombstones()
        releases = []
        for key, owner in list(self.items.items()):
            if (
                not owner.busy
                and not owner.removal_requested
                and time.monotonic() - owner.touched >= OWNER_IDLE_SECONDS
            ):
                self._start_release(key, owner)
                releases.append(self._bounded_release(owner))
        if releases:
            await asyncio.gather(*releases)

    def admit(
        self,
        key,
        fingerprint,
        ephemeral,
        retry_key=None,
        *,
        lineage=None,
        lease_refs=(),
    ):
        self._prune_tombstones()
        if retry_key is not None and retry_key in self.tombstones:
            terminal = self.terminal_failures.get(retry_key)
            if terminal is not None:
                raise _TerminalHTTPError(terminal)
            logger.warning(
                "bridge replay refused: identical request after an uncertain attempt"
            )
            record_event("admission_rejected", reason="replay")
            raise HTTPException(
                502, "Previous identical request failed; automatic replay refused"
            )
        owner = self.items.get(key)
        if owner is not None and (
            owner.busy
            or owner.removal_requested
            or (owner.close_task is not None and not owner.close_confirmed)
            or (owner.release_task is not None and not owner.release_task.done())
            or (owner.cleanup_started and not owner.cleanup_completed)
        ):
            record_event("admission_rejected", reason="owner_busy")
            raise HTTPException(
                409, "Owner already has an active request; no inference started"
            )
        created = owner is None
        if created:
            if self._capacity_used() >= self.limit:
                record_event("admission_rejected", reason="capacity", count=self._capacity_used(), capacity=self.limit)
                raise HTTPException(429, "Bridge owner capacity reached")
            owner = Owner(None, ephemeral=ephemeral)
            self.items[key] = owner
        assert owner is not None
        if lineage is not None:
            owner.lineage = lineage
            owner.lease_refs.update(lease_refs)
        owner.touched = time.monotonic()
        if owner.fingerprint == fingerprint:
            if owner.failed and not owner.replayable:
                if owner.terminal_error is not None:
                    raise _TerminalHTTPError(owner.terminal_error)
                logger.warning(
                    "bridge replay refused: identical request after an uncertain attempt"
                )
                record_event("admission_rejected", reason="replay")
                raise HTTPException(
                    502, "Previous identical request failed; automatic replay refused"
                )
            if owner.result is not None:
                owner.retirement_task = None
                owner.cleanup_started = False
                owner.cleanup_completed = False
                owner.cleanup_success = None
                owner.cleanup_deadline = None
                owner.busy = True
                return owner, True
        if owner.capacity_released and self._capacity_used() >= self.limit:
            record_event("admission_rejected", reason="capacity", count=self._capacity_used(), capacity=self.limit)
            raise HTTPException(429, "Bridge owner capacity reached")
        if owner.engine is None:
            try:
                engine = self.factory(hermes_home=self.home)
            except Exception:
                if created:
                    self.items.pop(key, None)
                record_event("admission_rejected", reason="engine_unavailable")
                raise HTTPException(503, "Bridge engine unavailable") from None
            owner.engine = engine
        owner.close_task = None
        owner.close_confirmed = False
        owner.capacity_released = False
        owner.retirement_task = None
        owner.cleanup_started = False
        owner.cleanup_completed = False
        owner.cleanup_success = None
        owner.cleanup_deadline = None
        owner.busy = True
        owner.fingerprint, owner.result, owner.failed = fingerprint, None, False
        owner.terminal_error = None
        owner.replayable = False
        owner.retry_key = retry_key
        return owner, False

    async def _retire(self, key, owner, success, uncertain=True):
        generation = owner.task
        try:
            if not success:
                owner.failed, owner.result = True, None
                owner.replayable = not uncertain
                if uncertain:
                    self._remember_failure(owner)
                if generation is not None:
                    generation.cancel()
                    await asyncio.gather(generation, return_exceptions=True)
                await _close(owner, bounded=False)
            if owner.ephemeral:
                owner.removal_requested = True
                confirmed = await _close(owner, bounded=False)
                if confirmed and self.items.get(key) is owner:
                    self.items.pop(key)
        finally:
            owner.busy = False
            owner.task = None
            owner.touched = time.monotonic()
            owner.cleanup_completed = True

    async def finish(self, key, owner, success, uncertain=True):
        if not owner.cleanup_started:
            owner.cleanup_started = True
            owner.cleanup_success = success
            owner.cleanup_deadline = (
                asyncio.get_running_loop().time() + CLOSE_TIMEOUT_SECONDS + 0.1
            )
            owner.retirement_task = asyncio.create_task(
                self._retire(key, owner, success, uncertain)
            )
        retirement = owner.retirement_task
        deadline = owner.cleanup_deadline
        assert retirement is not None and deadline is not None
        completed = await _join_cleanup(
            retirement, max(0.0, deadline - asyncio.get_running_loop().time())
        )
        if not completed and owner.engine is not None:
            if _resource_free_cleanup(owner.engine):
                owner.capacity_released = True

    async def _release_owner(self, key, owner):
        # Keep ownership until teardown is proven, even if the caller disappears.
        if owner.busy and owner.result is None:
            owner.failed = True
            self._remember_failure(owner)
        if owner.retirement_task is not None:
            await asyncio.shield(owner.retirement_task)
        if owner.task is not None:
            owner.task.cancel()
            await asyncio.gather(owner.task, return_exceptions=True)
        confirmed = await _close(owner, bounded=False)
        if confirmed and self.items.get(key) is owner:
            self.items.pop(key)

    async def close_owner(self, key):
        owner = self.items.get(key)
        if owner is None:
            return False
        self._start_release(key, owner)
        await self._bounded_release(owner)
        return True

    async def close_lease(self, lineage, lease):
        """Release one wrapper without retiring a still-referenced lineage binding."""
        matched = False
        releases = []
        for key, owner in list(self.items.items()):
            if owner.lineage != lineage or lease not in owner.lease_refs:
                continue
            matched = True
            owner.lease_refs.discard(lease)
            if not owner.lease_refs and not owner.removal_requested:
                self._start_release(key, owner)
                releases.append(self._bounded_release(owner))
        if releases:
            await asyncio.gather(*releases)
        return matched

    async def shutdown(self):
        owners = list(self.items.items())
        for key, owner in owners:
            self._start_release(key, owner)
        await asyncio.gather(
            *(self._bounded_release(owner) for _, owner in owners),
            return_exceptions=True,
        )


def _sse(value):
    return (
        "data: "
        + (value if isinstance(value, str) else json.dumps(value, allow_nan=False))
        + "\n\n"
    )


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI 2.4 reports disconnect through send(), outside the generator.
            try:
                await self.body_iterator.aclose()
            finally:
                await self.cleanup()


def create_app(token, home, engine_factory=None, owner_limit=None):
    """Build a lazy service. Factory accepts ``hermes_home=home``; no launch here."""
    if not isinstance(token, str) or not token.strip() or token != token.strip():
        raise ValueError("A nonempty bridge bearer credential is required")
    if engine_factory is None:
        from .client import NativeBridgeClient

        engine_factory = NativeBridgeClient
    owners = Owners(engine_factory, home, limit=owner_limit)

    @asynccontextmanager
    async def lifespan(app):
        async def reaper():
            while True:
                await asyncio.sleep(min(30, OWNER_IDLE_SECONDS))
                await owners.prune()

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await owners.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.owners = owners

    @app.exception_handler(_TerminalHTTPError)
    async def terminal_http_error(request, exc):
        return _error_response(exc.error)

    def authenticate(request):
        expected = ("Bearer " + token).encode()
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected
        ):
            raise HTTPException(
                401, "Invalid bridge credential", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/health")
    async def health(request: Request):
        authenticate(request)
        return {"service": "claude-native-bridge", "status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request):
        authenticate(request)
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "claude-native-bridge",
                }
                for model in MODELS
            ],
        }

    @app.post("/v1/owner/close")
    async def close_owner(request: Request):
        authenticate(request)
        key = request.headers.get("x-hermes-bridge-client", "")
        if not key or len(key) > 512:
            raise HTTPException(400, "Invalid owner identifier")
        lineage = _canonical_uuid(
            request.headers.get("x-hermes-bridge-retry-lineage", "")
        )
        closed = (
            await owners.close_lease(lineage, key)
            if lineage is not None
            else await owners.close_owner(key)
        )
        return {"closed": closed}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        authenticate(request)
        raw = bytearray()
        try:
            async with asyncio.timeout(30):
                async for piece in request.stream():
                    raw.extend(piece)
                    if len(raw) > MAX_BODY_BYTES:
                        raise HTTPException(413, "Request exceeds 8 MiB")
            body = json.loads(raw)
            _validate(body)
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(408, "Request body timeout") from None
        except Exception:
            raise HTTPException(
                400, "Invalid or unsupported completion request"
            ) from None
        owner_header = request.headers.get("x-hermes-bridge-client", "")
        if len(owner_header) > 512:
            raise HTTPException(400, "Invalid owner identifier")
        retry_lineage = _canonical_uuid(
            request.headers.get("x-hermes-bridge-retry-lineage", "")
        )
        binding = body.get("hermes_session_id")
        logical = bool(owner_header and binding and retry_lineage)
        ephemeral = not (owner_header and binding)
        key = (
            ("logical", retry_lineage, binding)
            if logical
            else (uuid.uuid4().hex if ephemeral else owner_header)
        )
        fingerprint = hashlib.sha256(
            json.dumps(body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        inference_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    name: value
                    for name, value in body.items()
                    if name not in {"stream", "stream_options", "response_format"}
                },
                sort_keys=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        raw_leases = request.headers.get("x-hermes-bridge-leases", "")
        lease_refs = {
            lease
            for value in raw_leases.split(",")[:128]
            if (lease := _canonical_uuid(value.strip())) is not None
        }
        if logical:
            lease_refs.add(owner_header)
        retry_key = (
            (
                retry_lineage,
                body["hermes_session_id"],
                inference_fingerprint,
            )
            if isinstance(body.get("hermes_session_id"), str)
            and body["hermes_session_id"]
            and retry_lineage
            else None
        )
        await owners.prune()
        owner, cached = owners.admit(
            key,
            fingerprint,
            ephemeral,
            retry_key=retry_key,
            lineage=retry_lineage if logical else None,
            lease_refs=lease_refs,
        )
        streaming = body.get("stream", False)
        trace = uuid.uuid4().hex[:12]
        started = time.monotonic()
        record_event(
            "generation_started", trace=trace, stream=streaming,
            cached=cached, count=owners._capacity_used(), capacity=owners.limit,
        )

        def log_completion(result):
            choice = result["choices"][0]
            record_event(
                "generation_completed", trace=trace, stream=streaming,
                cached=cached, finish=choice["finish_reason"],
                tool_count=len(choice["message"]["tool_calls"] or []),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        events = queue.Queue(maxsize=1024)
        text_size = 0

        def on_text(delta):
            nonlocal text_size
            if not isinstance(delta, str):
                raise ValueError("Invalid native text delta")
            text_size += len(delta.encode())
            if text_size > MAX_BODY_BYTES:
                raise ValueError("Native stream exceeds bounded buffer")
            if delta:
                events.put_nowait(delta)

        async def invoke():
            kwargs = {
                k: v
                for k, v in body.items()
                if k
                not in {
                    "hermes_session_id",
                    "stream_options",
                    "stream",
                    "response_format",
                }
            }
            kwargs.update(
                stream=False,
                extra_body={"hermes_session_id": body.get("hermes_session_id")},
            )
            if streaming:
                kwargs["_on_text"] = on_text
            with event_trace(trace):
                result = await asyncio.to_thread(
                    owner.engine.chat.completions.create, **kwargs
                )
            if inspect.isawaitable(result):
                result = await result
            return _completion(result, body)

        if cached:
            if not streaming:
                owner.busy = False
                log_completion(owner.result)
                return JSONResponse(owner.result)
        else:
            owner.task = asyncio.create_task(invoke())
        task = owner.task
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS

        async def check():
            if await request.is_disconnected():
                raise ConnectionError("HTTP disconnected")
            if time.monotonic() > deadline:
                raise TimeoutError("Generation timeout")

        if not streaming:
            success = False
            uncertain = True
            try:
                while not task.done():
                    await check()
                    await asyncio.sleep(0.02)
                await check()
                result = await task
                owner.result, success = result, True
                log_completion(result)
                return JSONResponse(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                uncertain = not _not_delivered(exc)
                _log_generation_failure(exc, uncertain, trace=trace, stream=streaming, started=started)
                terminal = _terminal_error(exc)
                if terminal is not None:
                    owner.terminal_error = terminal
                    return _error_response(terminal)
                raise HTTPException(
                    502, "Native generation failed or disconnected"
                ) from None
            finally:
                await owners.finish(key, owner, success, uncertain=uncertain)

        stream_state = {
            "success": False,
            "uncertain": True,
            "cleanup_started": False,
            "cleanup_completed": False,
        }

        async def cleanup():
            if stream_state["cleanup_completed"]:
                return
            stream_state["cleanup_started"] = True
            try:
                await owners.finish(
                    key,
                    owner,
                    stream_state["success"],
                    uncertain=stream_state["uncertain"],
                )
            finally:
                stream_state["cleanup_completed"] = owner.cleanup_completed

        async def stream():
            streamed = ""
            stream_id = owner.result["id"] if cached else "chatcmpl-" + uuid.uuid4().hex
            created = owner.result["created"] if cached else int(time.time())

            def chunk(
                delta=None,
                finish=None,
                usage=None,
                provenance=None,
                compaction=None,
                rotation=None,
                unexpected_compaction=None,
            ):
                value = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body["model"],
                    "choices": []
                    if usage is not None
                    else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                    "usage": usage,
                }
                if provenance is not None:
                    value[_PROVENANCE_FIELD] = provenance
                if compaction is not None:
                    value[_COMPACTION_FIELD] = compaction
                if rotation is not None:
                    value[_ROTATION_FIELD] = rotation
                if unexpected_compaction is not None:
                    value[_UNEXPECTED_COMPACTION_FIELD] = unexpected_compaction
                return value

            try:
                yield _sse(chunk({"role": "assistant"}))
                if not cached:
                    while True:
                        await check()
                        while not events.empty():
                            delta = events.get_nowait()
                            streamed += delta
                            yield _sse(chunk({"content": delta}))
                        if task.done():
                            break
                        await asyncio.sleep(0.01)
                    result = await task
                else:
                    result = owner.result
                assert result is not None
                message = result["choices"][0]["message"]
                content = message["content"] or ""
                if not content.startswith(streamed):
                    raise ValueError("Native streamed text diverged from final content")
                remainder = content[len(streamed) :]
                if remainder:
                    yield _sse(chunk({"content": remainder}))
                if message["tool_calls"]:
                    yield _sse(
                        chunk(
                            {
                                "tool_calls": [
                                    dict(call, index=i)
                                    for i, call in enumerate(message["tool_calls"])
                                ]
                            }
                        )
                    )
                provenance = result.get(_PROVENANCE_FIELD)
                compaction = result.get(_COMPACTION_FIELD)
                rotation = result.get(_ROTATION_FIELD)
                unexpected_compaction = result.get(_UNEXPECTED_COMPACTION_FIELD)
                include_usage = (
                    (body.get("stream_options") or {}).get("include_usage")
                    and result["usage"] is not None
                )
                yield _sse(
                    chunk(
                        finish=result["choices"][0]["finish_reason"],
                        provenance=None if include_usage else provenance,
                        compaction=None if include_usage else compaction,
                        rotation=None if include_usage else rotation,
                        unexpected_compaction=(
                            None if include_usage else unexpected_compaction
                        ),
                    )
                )
                if include_usage:
                    yield _sse(
                        chunk(
                            usage=result["usage"],
                            provenance=provenance,
                            compaction=compaction,
                            rotation=rotation,
                            unexpected_compaction=unexpected_compaction,
                        )
                    )
                yield _sse("[DONE]")
                if not cached:
                    # Keep the wire ID stable for an exact replay.
                    result["id"], result["created"] = stream_id, created
                    owner.result = result
                stream_state["success"] = True
                log_completion(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stream_state["uncertain"] = not _not_delivered(exc)
                _log_generation_failure(exc, stream_state["uncertain"], trace=trace, stream=streaming, started=started)
                terminal = _terminal_error(exc)
                if terminal is not None:
                    owner.terminal_error = terminal
                yield _sse(
                    {
                        "error": terminal
                        or {
                            "message": "Native generation failed or disconnected",
                            "type": "bridge_generation_error",
                            "code": "generation_failed",
                        }
                    }
                )
            finally:
                await cleanup()

        return _ClosingStreamingResponse(
            stream(),
            cleanup=cleanup,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
