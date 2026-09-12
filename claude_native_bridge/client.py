"""Hermes' OpenAI-shaped client backed by isolated native Claude sessions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import yaml

from .native import MODELS, NativeSession, NativeSessionLost
from .protocol import HistoryTracker, build_completion
from .settings import NativeBridgeError, Settings


@dataclass
class Binding:
    history: HistoryTracker
    native: NativeSession | None = None
    model: str = ""
    effort: str = ""


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


def _page_tool_results(messages, native, threshold):
    paged = copy.deepcopy(messages)
    oversized = [
        message
        for message in paged
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and isinstance(message.get("content"), str)
        and len(message["content"]) > threshold
    ]
    if not oversized:
        return paged
    runtime = getattr(native, "runtime", None)
    if runtime is None:
        raise NativeBridgeError("Native runtime unavailable for paged tool result")
    for message in oversized:
        text = message["content"]
        handle = _write_spool(Path(runtime), text)
        message["content"] = (
            f"Result too large ({len(text):,} chars). Handle: {handle}.\n"
            f'Call read_result(handle="{handle}", offset=0, length=15000) '
            "to read it in pages."
        )
    return paged


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
        # Real HTTP pool deliberately exposed under the usual OpenAI attribute:
        # Hermes' existing socket-shutdown path can interrupt local bridge I/O.
        self._client = httpx.Client(trust_env=False, timeout=12)
        self._invoke_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._bindings = {}
        self._closed = False
        self._active = False
        self._cancel = threading.Event()
        self.chat = NS(completions=NS(create=self.create))

    @property
    def is_closed(self):
        return self._closed

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
            self.close()
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
                    if name != key and value.native is not None and value.native.closed
                ]
                for name in expired:
                    self._bindings.pop(name)
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
                if state.native is not None and (
                    state.native.closed
                    or state.model != model
                    or state.effort != effort
                ):
                    state.native.close()
                    state.native = None
                    state.history.reset()

                frame = None
                paged_messages = None
                if state.native is not None:
                    paged_messages = _page_tool_results(
                        messages, state.native, settings.page_threshold
                    )
                    frame = state.history.prepare(paged_messages, tools, choice)
                    if frame["reset"]:
                        state.native.close()
                        state.native = None

                if state.native is None:
                    native = self._native_factory(
                        settings, self._home, model, effort, http_client=self._client
                    )
                    native.hermes_binding = binding
                    with self._state_lock:
                        state.native = native
                    state.model = model
                    state.effort = effort
                    native.start()
                    paged_messages = _page_tool_results(
                        messages, native, settings.page_threshold
                    )
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
                completion.native_bridge_response_source = getattr(
                    state.native, "last_response_source", "respond"
                )
                state.history.commit(
                    paged_messages, tools, choice, assistant_dict(completion)
                )
                return (
                    CompletedStream(completion) if kwargs.get("stream") else completion
                )
            except NativeSessionLost:
                if state.native:
                    state.native.close()
                state.native = None
                state.history.reset()
                raise
            except NativeBridgeError as exc:
                native = state.native
                session_lost = False
                health = getattr(native, "health", None)
                if native is not None and exchange_started and callable(health):
                    try:
                        session_lost = not health()
                    except BaseException:
                        # An inconclusive liveness probe is still an ambiguous failure.
                        session_lost = False
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
                self.close()
                raise
            except BaseException:
                # Once a response is uncertain, never retry against this hidden native state.
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
                return
            self._closed = True
            self._cancel.set()
            states = list(self._bindings.values())
            active = self._active
        # Native group shutdown gives in-flight HTTP a real EOF; the owner thread
        # releases socket FDs afterward, rather than closing FDs underneath it.
        for state in states:
            if state.native:
                state.native.close()
        if not active:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
