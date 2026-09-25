"""Hermes' OpenAI-shaped client backed by isolated native Claude sessions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS

import httpcore
import httpx
import yaml

from .native import MODELS, NativeSession, NativeSessionLost
from .event_log import failure_reason, record_event, safe_error_type, safe_run
from .protocol import HistoryTracker, build_completion
from .settings import (
    ASSUMED_CONTEXT_WINDOW,
    NativeBridgeError,
    NativeRequestNotDelivered,
    Settings,
    rotation_threshold,
)
from .usage import completion_usage_provenance

logger = logging.getLogger(__name__)

ASYNC_CLOSE_TIMEOUT_SECONDS = 5.0
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


class _HostAbortSocket:
    """Notify the bridge when Hermes shuts down an in-flight pool socket."""

    def __init__(self, socket, abort):
        self._socket = socket
        self._abort = abort

    def settimeout(self, value):
        return self._socket.settimeout(value)

    def shutdown(self, how):
        try:
            return self._socket.shutdown(how)
        finally:
            # On Windows a stranger-thread shutdown can abort the peer yet leave
            # the owning httpx recv blocked. Retire the native peer as well; its
            # death is what makes the owner unwind without cross-thread FD close.
            self._abort()

    def __getattr__(self, name):
        return getattr(self._socket, name)


class _HostAbortStream(httpcore.NetworkStream):
    """Transparent httpcore stream whose exposed socket carries cancellation."""

    def __init__(self, stream, abort):
        self._stream = stream
        self._abort = abort
        self._abort_socket = None

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self._stream.write(buffer, timeout)

    def close(self):
        return self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _HostAbortStream(
            self._stream.start_tls(ssl_context, server_hostname, timeout),
            self._abort,
        )

    def get_extra_info(self, info):
        value = self._stream.get_extra_info(info)
        if info != "socket" or value is None:
            return value
        if self._abort_socket is None or self._abort_socket._socket is not value:
            self._abort_socket = _HostAbortSocket(value, self._abort)
        return self._abort_socket


class _HostAbortBackend(httpcore.NetworkBackend):
    """Delegate network creation and wrap only the returned stream."""

    def __init__(self, backend, abort):
        self._backend = backend
        self._abort = abort

    def connect_tcp(self, *args, **kwargs):
        return _HostAbortStream(
            self._backend.connect_tcp(*args, **kwargs), self._abort
        )

    def connect_unix_socket(self, *args, **kwargs):
        return _HostAbortStream(
            self._backend.connect_unix_socket(*args, **kwargs), self._abort
        )

    def sleep(self, seconds):
        return self._backend.sleep(seconds)


def _host_abort_aware_http_client(abort):
    """Build the local-only pool using httpcore's network-backend seam."""
    transport = httpx.HTTPTransport()
    pool = transport._pool
    pool._network_backend = _HostAbortBackend(pool._network_backend, abort)
    return httpx.Client(trust_env=False, timeout=12, transport=transport)


def _safe_compaction(value):
    """Validate the native projection again before attaching public metadata."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    trigger = value.get("trigger")
    request_id = value.get("request_id")
    active = value.get("active_request")
    generation = value.get("generation")
    summary_bytes = value.get("summary_bytes")
    error = value.get("error")
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


async def _join_async_close(task, allowance):
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


@dataclass
class Binding:
    history: HistoryTracker
    native: NativeSession | None = None
    model: str = ""
    effort: str = ""
    source_history: HistoryTracker | None = None
    compacted_messages: list[dict] | None = None
    # Rotation evidence for the current native session only.
    context: dict | None = None
    sent_chars: int = 0
    exchanges: int = 0


@dataclass(frozen=True)
class ClientCleanupOutcome:
    """API-facing evidence that all native cleanup attempts have settled."""

    bindings_attempted: int
    released_bindings: tuple[str, ...]
    uncertain_bindings: tuple[str, ...]
    http_closed: bool
    errors: tuple[str, ...]

    @property
    def safe_to_release_capacity(self):
        return not self.uncertain_bindings


_NOT_DELIVERED_PREFIX = (
    "Native request was not delivered; the engine was retired before any native "
    "input. A retry rebuilds from canonical history."
)


def _not_delivered_error(exc):
    """Tag a failure as replayable while keeping its specific diagnostic."""
    detail = str(exc).strip() or type(exc).__name__
    return NativeRequestNotDelivered(_NOT_DELIVERED_PREFIX + " Cause: " + detail[:200])


def _record_native_failure(native, exc, branch):
    """Best-effort bounded evidence in the session's private runtime directory.

    Records our own branch label, the exception class name and a bounded
    exception message. The directory already holds native output; nothing here
    is promoted into logs or the public API.
    """
    runtime = getattr(native, "runtime", None)
    run = safe_run(runtime)
    fields = {
        "branch": branch,
        "error_type": safe_error_type(exc),
        "reason": failure_reason(exc),
    }
    if run is not None:
        fields["run"] = run
    record_event("native_failure", **fields)
    write = getattr(native, "_private_json", None)
    if runtime is None or not callable(write):
        return
    try:
        write(
            "native-failure.json",
            {
                "branch": branch,
                "error": type(exc).__name__,
                "message": str(exc)[:200],
            },
        )
    except BaseException:
        pass


def _physical_cleanup_outcome(native, returned=None):
    outcome = returned or getattr(native, "cleanup_outcome", None)
    if hasattr(outcome, "safe_to_release_capacity"):
        return outcome
    # Lightweight/non-physical test engines have no runtime capability at all.
    # This compatibility path does not treat a physical engine's closed flag as
    # death evidence: any runtime-aware engine must publish an explicit outcome.
    if not hasattr(native, "runtime") and getattr(native, "closed", False):
        return NS(safe_to_release_capacity=True)
    return None


class CompletedStream:
    """One buffered chunk, explicitly not live token streaming."""

    def __init__(self, completion):
        msg = completion.choices[0].message
        calls = [
            NS(index=i, id=tc.id, type="function", function=tc.function)
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        delta = NS(role="assistant", content=msg.content, tool_calls=calls or None)
        self.chunk = NS(
            id=completion.id,
            object="chat.completion.chunk",
            created=completion.created,
            model=completion.model,
            choices=[
                NS(
                    index=0,
                    delta=delta,
                    finish_reason=completion.choices[0].finish_reason,
                )
            ],
            usage=completion.usage,
        )
        self.done = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.done:
            raise StopIteration
        self.done = True
        return self.chunk

    def close(self):
        self.done = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        self.close()


def assistant_dict(completion):
    m = completion.choices[0].message
    result = {"role": "assistant", "content": m.content}
    if m.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in m.tool_calls
        ]
    return result


def _write_spool(runtime: Path, text: str) -> str:
    encoded = text.encode("utf-8")
    handle = "r" + hashlib.sha256(encoded).hexdigest()[:32]
    spool = Path(runtime) / "spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(spool, 0o700)
    target = spool / (handle + ".txt")
    if target.exists():
        if target.read_bytes() != encoded:
            raise NativeBridgeError("Paged result handle collision")
        return handle
    temporary = spool / ("." + handle + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return handle


def _with_session_identity(messages, binding):
    native_messages = copy.deepcopy(messages)
    if binding:
        native_messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    "[Bridge metadata] Canonical Hermes session ID: "
                    f"{binding}. Bridge runtime directory names are opaque "
                    "and are not Hermes session IDs."
                ),
            },
        )
    return native_messages


def _page_tool_results(messages, native, threshold):
    paged = copy.deepcopy(messages)
    oversized = set()
    batch = []
    batch_chars = 0

    def finish_batch():
        nonlocal batch, batch_chars
        if batch_chars > threshold:
            oversized.update(batch)
        batch = []
        batch_chars = 0

    for index, message in enumerate(paged):
        if (
            isinstance(message, dict)
            and message.get("role") == "tool"
            and isinstance(message.get("content"), str)
        ):
            batch.append(index)
            batch_chars += len(message["content"])
            if len(message["content"]) > threshold:
                oversized.add(index)
        else:
            finish_batch()
    finish_batch()

    if not oversized:
        return paged
    runtime = getattr(native, "runtime", None)
    if runtime is None:
        raise NativeBridgeError("Native runtime unavailable for paged tool result")
    for index in sorted(oversized):
        message = paged[index]
        text = message["content"]
        handle = _write_spool(Path(runtime), text)
        message["content"] = (
            f"Result too large ({len(text):,} chars). Handle: {handle}.\n"
            f'Call read_result(handle="{handle}", offset=0, length=15000) '
            "to read it in pages."
        )
    return paged


def _bounded_bootstrap(messages, tools, choice, native, maximum):
    """Bound a bootstrap without dropping instructions or splitting exchanges."""
    tracker = HistoryTracker()
    if len(tracker.prepare(messages, tools, choice)["content"]) <= maximum:
        return copy.deepcopy(messages), False

    identity = []
    history = messages
    if (
        messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
        and isinstance(messages[0].get("content"), str)
        and messages[0]["content"].startswith(
            "[Bridge metadata] Canonical Hermes session ID:"
        )
    ):
        identity = [copy.deepcopy(messages[0])]
        history = messages[1:]

    # A tool proposal and its contiguous result batch are one history unit. A
    # cutoff may retain or spool the whole unit, but never expose orphaned calls
    # or results to the native session.
    groups = []
    index = 0
    while index < len(history):
        message = history[index]
        group = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            index += 1
            while index < len(history) and history[index].get("role") == "tool":
                group.append(history[index])
                index += 1
        else:
            index += 1
        groups.append(
            {
                "messages": group,
                "mandatory": any(
                    item.get("role") in ("system", "developer") for item in group
                ),
            }
        )

    current_start = next(
        (
            group_index
            for group_index in range(len(groups) - 1, -1, -1)
            if any(
                message.get("role") == "user"
                for message in groups[group_index]["messages"]
            )
        ),
        len(groups),
    )
    required = [*identity]
    for group_index, group in enumerate(groups):
        if group["mandatory"] or group_index >= current_start:
            required.extend(copy.deepcopy(group["messages"]))
    if len(tracker.prepare(required, tools, choice)["content"]) > maximum:
        raise NativeBridgeError(
            "bootstrap_max_chars is too small for the mandatory instruction frame "
            "and current user task"
        )

    runtime = getattr(native, "runtime", None)
    if runtime is None:
        raise NativeBridgeError("Native runtime unavailable for bounded bootstrap")

    def omission(messages_to_omit):
        serialized = json.dumps(
            messages_to_omit, ensure_ascii=False, separators=(",", ":")
        )
        predicted = (
            "r" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
        )
        notice = {
            "role": "system",
            "content": (
                f"[Earlier conversation omitted: {len(serialized)} chars. "
                f"Ask read_result handle '{predicted}' for older windows if needed.]"
            ),
        }
        return serialized, predicted, notice

    def candidate(start):
        bounded = copy.deepcopy(identity)
        omitted = []
        spools = []

        def flush_omitted():
            nonlocal omitted
            if omitted:
                serialized, predicted, notice = omission(omitted)
                bounded.append(notice)
                spools.append((serialized, predicted))
                omitted = []

        for group_index, group in enumerate(groups):
            if (
                group["mandatory"]
                or group_index >= current_start
                or group_index >= start
            ):
                flush_omitted()
                bounded.extend(copy.deepcopy(group["messages"]))
            else:
                omitted.extend(copy.deepcopy(group["messages"]))
        flush_omitted()
        return bounded, spools

    selected = None
    for start in range(1, len(groups) + 1):
        current = candidate(start)
        if len(tracker.prepare(current[0], tools, choice)["content"]) <= maximum:
            selected = current
            break

    if selected is None:
        raise NativeBridgeError(
            "bootstrap_max_chars is too small for the mandatory instruction "
            "frame and omission notices"
        )

    bounded, spools = selected
    for serialized, predicted in spools:
        handle = _write_spool(Path(runtime), serialized)
        if handle != predicted:
            raise NativeBridgeError("Bounded bootstrap handle mismatch")
    return bounded, True


def _clear_bootstrap_tail(state):
    state.source_history = None
    state.compacted_messages = None


def _bounded_utf8_bytes(text, limit):
    """Count UTF-8 bytes only until admission is already known to fail."""
    total = 0
    for offset in range(0, len(text), 4096):
        total += len(text[offset : offset + 4096].encode("utf-8"))
        if total >= limit:
            return limit, True
    return total, False


def _rotation_due(state, settings, incoming_frame):
    """Decide between requests whether to retire the native session.

    Only Claude's own status-line counters drive token occupancy. The next
    serialized frame is admitted with a bounded UTF-8 byte upper bound that is
    explicitly not native token telemetry. Without correlated counters, exact
    serialized character growth is bounded instead. Rotation is evaluated once
    between exchanges, so a rebuild cannot recursively rotate itself.
    """
    if state.native is None or state.exchanges == 0:
        return None
    context = state.context
    if context is not None and context.get("telemetry_exchange") == state.exchanges:
        reported = context.get("window")
        window = reported or ASSUMED_CONTEXT_WINDOW
        threshold = rotation_threshold(settings, window)
        evidence = {
            "rotated": True,
            "reason": "context_tokens",
            "observed_tokens": context["tokens"],
            "threshold_tokens": threshold,
            "window_tokens": window,
            "window_source": "native_status_line" if reported else "assumed",
            "native_exchanges": state.exchanges,
            "telemetry_exchange": context["telemetry_exchange"],
        }
        if context["tokens"] >= threshold:
            return evidence
        remaining = threshold - context["tokens"]
        incoming_bytes, saturated = _bounded_utf8_bytes(
            incoming_frame, remaining + 1
        )
        if incoming_bytes <= remaining:
            return None
        evidence["reason"] = "incoming_admission"
        evidence["incoming_estimate"] = {
            "bytes": incoming_bytes,
            "source": "utf8_bytes_conservative_bound",
            "native_tokens": None,
            "saturated": saturated,
        }
        return evidence

    remaining = settings.rotation_fallback_chars - state.sent_chars
    incoming_chars = min(len(incoming_frame), max(remaining + 1, 0))
    if state.sent_chars < settings.rotation_fallback_chars and incoming_chars <= remaining:
        return None
    return {
        "rotated": True,
        "reason": "uncorrelated_usage_chars",
        "observed_chars": state.sent_chars,
        "threshold_chars": settings.rotation_fallback_chars,
        "native_exchanges": state.exchanges,
        "incoming_estimate": {
            "chars": incoming_chars,
            "source": "serialized_frame_chars",
            "native_tokens": None,
            "saturated": incoming_chars < len(incoming_frame),
        },
    }


class NativeBridgeClient:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, **kwargs):
        # Discovery and client construction must not launch Claude or validate login.
        self.api_key = kwargs.get("api_key") or "external-process"
        self.base_url = kwargs.get("base_url") or "claude-native://bridge"
        self.timeout = kwargs.get("timeout")
        self._settings = kwargs.get("settings")
        self._home = kwargs.get("hermes_home")
        self._native_factory = kwargs.get("native_factory", NativeSession)
        self._invoke_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._host_abort_started = False
        self._host_abort_errors = []
        self._closed = False
        self._active = False
        self._cancel = threading.Event()
        # Real HTTP pool deliberately exposed under the usual OpenAI attribute:
        # Hermes' socket shutdown also reaches the plugin cancellation callback.
        self._client = _host_abort_aware_http_client(self._abort_from_host_socket)
        self._bindings = {}
        self._async_close_task = None
        self.cleanup_outcome: ClientCleanupOutcome | None = None
        self.chat = NS(completions=NS(create=self.create))

    @property
    def is_closed(self):
        return self._closed

    @property
    def cleanup_resource_free(self):
        """API owner seam: physical capacity is free, even if close is settling."""
        outcome = self.cleanup_outcome
        return bool(outcome is not None and outcome.safe_to_release_capacity)

    @property
    def cleanup_confirmed(self):
        """API owner seam used after close returns or raises."""
        return self.cleanup_resource_free

    def _abort_from_host_socket(self):
        """Fence the request and retire its native peer without closing TCP FDs."""
        self._cancel.set()
        with self._state_lock:
            if self._host_abort_started or self._closed:
                return
            self._host_abort_started = True
            natives = tuple(
                state.native
                for state in self._bindings.values()
                if state.native is not None
            )

        def retire_native_peers():
            for native in natives:
                close = getattr(native, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        # The inference owner performs the authoritative close
                        # and publishes cleanup evidence while unwinding.
                        self._host_abort_errors.append(
                            f"{type(exc).__name__}: {exc}"
                        )

        threading.Thread(
            target=retire_native_peers,
            name="hcb-host-abort",
            daemon=True,
        ).start()

    def _configuration(self):
        if self._home is None:
            from hermes_constants import get_hermes_home

            self._home = Path(get_hermes_home())
        else:
            self._home = Path(self._home)
        if self._settings is None:
            path = self._home / "config.yaml"
            cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
            self._settings = Settings.from_mapping(
                (cfg or {}).get("claude_native_bridge", {})
            )
        self._settings.check_consent()
        return self._settings

    def create(self, **kwargs):
        """Internal _on_text(delta: str) runs synchronously on the inference thread.

        It receives nonempty native batches, verbatim and once, before completion.
        It must return promptly or raise to abort. No reconstructed final is sent
        through this callback. The returned completion still includes all text.
        """
        callback = kwargs.get("_on_text")
        if callback is not None and not callable(callback):
            raise TypeError("_on_text must be a synchronous callable")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._create_sync(**kwargs)
        return self._create_async(**kwargs)

    async def _create_async(self, **kwargs):
        try:
            return await asyncio.to_thread(self._create_sync, **kwargs)
        except asyncio.CancelledError:
            if self._async_close_task is None:
                self._async_close_task = asyncio.create_task(
                    asyncio.to_thread(self.close)
                )
            await _join_async_close(self._async_close_task, ASYNC_CLOSE_TIMEOUT_SECONDS)
            raise

    def _create_sync(self, **kwargs):
        with self._invoke_lock:
            if self._closed:
                raise NativeBridgeError(
                    "Native bridge client is closed; start a new Hermes request context."
                )
            settings = self._configuration()
            model = kwargs.get("model")
            if model not in MODELS:
                raise NativeBridgeError("Unverified native model: " + str(model))
            extra = kwargs.get("extra_body") or {}
            effort = (
                kwargs.get("reasoning_effort")
                or extra.get("hermes_native_effort")
                or settings.effort
            )
            if effort not in ("low", "medium", "high", "xhigh", "max"):
                raise NativeBridgeError("Unsupported native effort: " + str(effort))
            binding = extra.get("hermes_session_id")
            if binding is not None and not isinstance(binding, str):
                raise NativeBridgeError("Invalid Hermes session binding")
            # No binding means a potentially shared auxiliary client. Isolate each call.
            ephemeral = not binding
            key = binding or "aux-" + str(uuid.uuid4())
            messages = kwargs.get("messages", [])
            tools = kwargs.get("tools")
            choice = kwargs.get("tool_choice")
            with self._state_lock:
                expired = [
                    name
                    for name, value in self._bindings.items()
                    if name != key
                    and (
                        value.native is None
                        or bool(
                            getattr(
                                _physical_cleanup_outcome(value.native),
                                "safe_to_release_capacity",
                                False,
                            )
                        )
                    )
                ]
                for name in expired:
                    expired_state = self._bindings.pop(name)
                    expired_state.history.reset()
                if (
                    len(self._bindings) >= settings.max_sessions
                    and key not in self._bindings
                ):
                    raise NativeBridgeError(
                        "Native session limit reached; close another conversation before starting one."
                    )
                state = self._bindings.setdefault(key, Binding(HistoryTracker()))
                self._active = True
            exchange_started = False
            try:
                # Validate canonical input before any native startup or spool write.
                HistoryTracker().prepare(messages, tools, choice)
                messages = _with_session_identity(messages, binding)
                switching = False
                rotation = None
                if state.native is not None and (
                    state.native.closed
                    or state.model != model
                    or state.effort != effort
                ):
                    switching = (
                        state.model != model
                        or state.effort != effort
                    )
                    state.native.close()
                    state.native = None
                    state.history.reset()
                    if switching:
                        _clear_bootstrap_tail(state)

                frame = None
                paged_messages = None
                source_messages = None
                if state.native is not None:
                    paged_source = _page_tool_results(
                        messages, state.native, settings.page_threshold
                    )
                    if state.source_history is not None:
                        source_frame = state.source_history.prepare(
                            paged_source, tools, choice
                        )
                        if source_frame["reset"]:
                            _clear_bootstrap_tail(state)
                            paged_messages, _ = _bounded_bootstrap(
                                paged_source,
                                tools,
                                choice,
                                state.native,
                                settings.bootstrap_max_chars,
                            )
                        else:
                            delta = json.loads(source_frame["content"])["messages"]
                            paged_messages = copy.deepcopy(
                                state.compacted_messages or []
                            ) + delta
                            source_messages = paged_source
                    else:
                        paged_messages = paged_source
                    frame = state.history.prepare(paged_messages, tools, choice)
                    if frame["reset"]:
                        state.native.close()
                        state.native = None
                    else:
                        rotation = _rotation_due(
                            state, settings, frame["content"]
                        )
                        if rotation is not None:
                            state.native.close()
                            state.native = None
                            state.history.reset()
                            _clear_bootstrap_tail(state)

                if state.native is None:
                    native = self._native_factory(
                        settings, self._home, model, effort, http_client=self._client
                    )
                    native.hermes_binding = binding
                    with self._state_lock:
                        state.native = native
                    state.model = model
                    state.effort = effort
                    state.context = None
                    state.sent_chars = 0
                    state.exchanges = 0
                    native.start()
                    paged_source = _page_tool_results(
                        messages, native, settings.page_threshold
                    )
                    if state.source_history is not None:
                        source_frame = state.source_history.prepare(
                            paged_source, tools, choice
                        )
                        if source_frame["reset"]:
                            _clear_bootstrap_tail(state)
                            paged_messages, _ = _bounded_bootstrap(
                                paged_source,
                                tools,
                                choice,
                                native,
                                settings.bootstrap_max_chars,
                            )
                        else:
                            delta = json.loads(source_frame["content"])["messages"]
                            paged_messages = copy.deepcopy(
                                state.compacted_messages or []
                            ) + delta
                            source_messages = paged_source
                    else:
                        # Fresh sessions and divergence rebuilds both bootstrap
                        # from a full source; bound whichever exceeds the limit.
                        paged_messages, compacted = _bounded_bootstrap(
                            paged_source,
                            tools,
                            choice,
                            native,
                            settings.bootstrap_max_chars,
                        )
                        if compacted:
                            state.source_history = HistoryTracker()
                            state.compacted_messages = copy.deepcopy(paged_messages)
                            source_messages = paged_source
                        else:
                            paged_messages = paged_source
                    frame = state.history.prepare(paged_messages, tools, choice)
                assert frame is not None and paged_messages is not None
                request_id = str(uuid.uuid4())
                exchange_started = True
                response = state.native.exchange(
                    frame["content"],
                    request_id,
                    cancel_check=self._cancel.is_set,
                    on_text=kwargs.get("_on_text"),
                )
                state.exchanges += 1
                state.sent_chars += len(frame["content"])
                context = getattr(state.native, "last_context", None)
                state.context = None
                if (
                    isinstance(context, dict)
                    and type(context.get("tokens")) is int
                    and context["tokens"] >= 0
                    and (
                        context.get("window") is None
                        or (
                            type(context.get("window")) is int
                            and context["window"] > 0
                        )
                    )
                ):
                    state.context = {
                        "tokens": context["tokens"],
                        "window": context.get("window"),
                        "telemetry_exchange": state.exchanges,
                    }
                # Sequence belongs to local transport, not the model decision schema.
                decision = {k: v for k, v in response.items() if k != "sequence"}
                completion = build_completion(
                    decision, request_id, model, tools, choice
                )
                if decision.get("kind") == "tool_calls":
                    completion.choices[0].message.content = (
                        getattr(state.native, "last_text", "") or None
                    )
                completion.usage = getattr(state.native, "last_usage", None)
                completion.native_bridge_usage_provenance = (
                    completion_usage_provenance(
                        model,
                        completion.usage,
                        runtime=getattr(state.native, "runtime", None),
                        session_id=getattr(state.native, "session_id", None),
                    )
                )
                completion.native_bridge_response_source = getattr(
                    state.native, "last_response_source", "respond"
                )
                compaction = _safe_compaction(
                    getattr(state.native, "last_compaction", None)
                )
                if compaction is not None:
                    completion.native_bridge_compaction = compaction
                    if (
                        not settings.native_auto_compact
                        and compaction["trigger"] == "auto"
                    ):
                        # Sentinel: native auto-compaction ran although disabled.
                        completion.native_bridge_unexpected_compaction = True
                if rotation is not None:
                    completion.native_bridge_rotation = rotation
                assistant = assistant_dict(completion)
                state.history.commit(paged_messages, tools, choice, assistant)
                if state.source_history is not None and source_messages is not None:
                    state.source_history.commit(source_messages, tools, choice, assistant)
                    state.compacted_messages = copy.deepcopy(paged_messages) + [
                        copy.deepcopy(assistant)
                    ]
                return (
                    CompletedStream(completion) if kwargs.get("stream") else completion
                )
            except NativeSessionLost:
                if state.native:
                    state.native.close()
                state.native = None
                state.history.reset()
                raise
            except NativeRequestNotDelivered as exc:
                # No native input exists for this attempt, so the caller may
                # retry the identical request; the next attempt rebuilds from
                # canonical history instead of resurrecting a native turn.
                logger.warning(
                    "native request failed before delivery: %s", type(exc).__name__
                )
                _record_native_failure(state.native, exc, "not_delivered")
                self.close()
                raise
            except NativeBridgeError as exc:
                native = state.native
                if not exchange_started:
                    # This request never reached a native session: the engine was
                    # retired before any native input, so the caller may retry it.
                    logger.warning(
                        "native request failed before delivery: %s", type(exc).__name__
                    )
                    _record_native_failure(native, exc, "not_delivered")
                    self.close()
                    raise _not_delivered_error(exc) from exc
                session_lost = False
                health = getattr(native, "health", None)
                if native is not None and callable(health):
                    try:
                        session_lost = not health()
                    except BaseException:
                        # An inconclusive liveness probe is still an ambiguous failure.
                        session_lost = False
                _record_native_failure(native, exc, "uncertain")
                if session_lost:
                    assert native is not None
                    native.close()
                    state.native = None
                    state.history.reset()
                    raise NativeSessionLost(
                        "Native session was lost; retry to rebuild from canonical history. "
                        "The uncertain in-flight request was not replayed."
                    ) from exc
                # Once a response is uncertain, never retry against this hidden native state.
                logger.warning(
                    "native generation uncertain: %s", type(exc).__name__
                )
                self.close()
                raise
            except BaseException as exc:
                if not exchange_started:
                    # Nothing was delivered for this request; see the branch above.
                    logger.warning(
                        "native request failed before delivery: %s", type(exc).__name__
                    )
                    _record_native_failure(state.native, exc, "not_delivered")
                    self.close()
                    raise _not_delivered_error(exc) from exc
                # Once a response is uncertain, never retry against this hidden native state.
                logger.warning("native generation failed: %s", type(exc).__name__)
                _record_native_failure(state.native, exc, "unexpected")
                self.close()
                raise
            finally:
                if ephemeral:
                    with self._state_lock:
                        self._bindings.pop(key, None)
                    if state.native:
                        state.native.close()
                with self._state_lock:
                    self._active = False
                    close_http = self._closed
                if close_http:
                    self._client.close()

    def close(self):
        with self._state_lock:
            if self._closed:
                outcome = self.cleanup_outcome
                if outcome is not None and outcome.errors:
                    raise NativeBridgeError(
                        "Bridge teardown completed with errors: "
                        + "; ".join(outcome.errors)
                    )
                return outcome
            self._closed = True
            self._cancel.set()
            states = list(self._bindings.items())

        errors = []
        released = []
        uncertain = []
        attempted = 0
        # Fence first, then settle every native independently. A failed early
        # binding must never prevent siblings or the shared HTTP pool closing.
        for name, state in states:
            native = state.native
            if native is None:
                released.append(name)
                continue
            attempted += 1
            returned = None
            existing = _physical_cleanup_outcome(native)
            close = getattr(native, "close", None)
            if not callable(close):
                if existing is None:
                    errors.append(f"binding {name}: missing close capability")
            else:
                try:
                    returned = close()
                except BaseException as exc:
                    errors.append(f"binding {name}: {type(exc).__name__}: {exc}")
            outcome = _physical_cleanup_outcome(native, returned)
            if outcome is not None and outcome.safe_to_release_capacity:
                released.append(name)
            else:
                uncertain.append(name)

        # Publish physical evidence before closing the non-capacity HTTP pool so
        # an API timeout can safely distinguish a dead native from a hung close.
        self.cleanup_outcome = ClientCleanupOutcome(
            bindings_attempted=attempted,
            released_bindings=tuple(released),
            uncertain_bindings=tuple(uncertain),
            http_closed=False,
            errors=tuple(errors),
        )

        http_closed = False
        try:
            self._client.close()
            http_closed = True
        except BaseException as exc:
            errors.append(f"HTTP client: {type(exc).__name__}: {exc}")

        outcome = ClientCleanupOutcome(
            bindings_attempted=attempted,
            released_bindings=tuple(released),
            uncertain_bindings=tuple(uncertain),
            http_closed=http_closed,
            errors=tuple(errors),
        )
        self.cleanup_outcome = outcome
        if errors:
            raise NativeBridgeError(
                "Bridge teardown completed with errors: " + "; ".join(errors)
            )
        return outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
