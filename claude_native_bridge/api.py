"""Authenticated local OpenAI transport; native engines never execute tools here.

The injected factory has the same keyword interface as NativeBridgeClient.
Limits belong to this service, not to native inference configuration.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import queue
import time
import threading
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .models import MODELS
from .protocol import _messages, _tool_definitions, _choice, _validate_arguments

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_OWNERS = 32
OWNER_IDLE_SECONDS = 600.0
REQUEST_TIMEOUT_SECONDS = 600.0
CLOSE_TIMEOUT_SECONDS = 5.0
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
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_BODY_BYTES:
        raise ValueError("Native result exceeds bounded cache")
    return result


@dataclass
class Owner:
    engine: object
    ephemeral: bool = False
    busy: bool = False
    touched: float = field(default_factory=time.monotonic)
    fingerprint: str | None = None
    result: dict | None = None
    failed: bool = False
    task: asyncio.Task | None = None


async def _close(owner):
    engine, owner.engine = owner.engine, None
    if engine is not None:
        # Cancellation must not queue behind a saturated inference executor.
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def resolve():
            if not done.done():
                done.set_result(None)

        def close():
            try:
                result = engine.close()
                if inspect.isawaitable(result):
                    asyncio.run(result)
            except Exception:
                pass  # Never leak engine diagnostics/prompts into server logs.
            finally:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve)

        threading.Thread(target=close, daemon=True, name="bridge-close").start()
        with suppress(TimeoutError):
            await asyncio.wait_for(done, CLOSE_TIMEOUT_SECONDS)


class Owners:
    def __init__(self, factory, home, limit=None):
        self.factory, self.home = factory, home
        self.limit = MAX_OWNERS if limit is None else limit
        self.items = {}

    async def prune(self):
        for key, owner in list(self.items.items()):
            if not owner.busy and time.monotonic() - owner.touched > OWNER_IDLE_SECONDS:
                self.items.pop(key, None)
                await _close(owner)

    def admit(self, key, fingerprint, ephemeral):
        owner = self.items.get(key)
        if owner is not None and owner.busy:
            raise HTTPException(
                409, "Owner already has an active request; no inference started"
            )
        if owner is None:
            if len(self.items) >= self.limit:
                raise HTTPException(429, "Bridge owner capacity reached")
            owner = Owner(None, ephemeral=ephemeral)
            self.items[key] = owner
        owner.touched = time.monotonic()
        if owner.fingerprint == fingerprint:
            if owner.failed:
                raise HTTPException(
                    502, "Previous identical request failed; automatic replay refused"
                )
            if owner.result is not None:
                owner.busy = True
                return owner, True
        if owner.engine is None:
            try:
                owner.engine = self.factory(hermes_home=self.home)
            except Exception:
                self.items.pop(key, None)
                raise HTTPException(503, "Bridge engine unavailable") from None
        owner.busy = True
        owner.fingerprint, owner.result, owner.failed = fingerprint, None, False
        return owner, False

    async def finish(self, key, owner, success):
        if not success:
            owner.failed, owner.result = True, None
            await _close(owner)
            if owner.task is not None:
                owner.task.cancel()
                await asyncio.gather(owner.task, return_exceptions=True)
        if owner.ephemeral:
            await _close(owner)
            self.items.pop(key, None)
        owner.busy = False
        owner.task = None
        owner.touched = time.monotonic()

    async def close_owner(self, key):
        owner = self.items.pop(key, None)
        if owner is None:
            return False
        await _close(owner)
        if owner.task is not None:
            owner.task.cancel()
            await asyncio.gather(owner.task, return_exceptions=True)
        return True

    async def shutdown(self):
        owners = list(self.items.values())
        self.items.clear()
        await asyncio.gather(*(_close(owner) for owner in owners))
        for owner in owners:
            if owner.task is not None:
                owner.task.cancel()
        await asyncio.gather(
            *(o.task for o in owners if o.task is not None), return_exceptions=True
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
        return {"closed": await owners.close_owner(key)}

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
        ephemeral = not (owner_header and body.get("hermes_session_id"))
        key = uuid.uuid4().hex if ephemeral else owner_header
        fingerprint = hashlib.sha256(
            json.dumps(body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        await owners.prune()
        owner, cached = owners.admit(key, fingerprint, ephemeral)
        streaming = body.get("stream", False)
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
            result = await asyncio.to_thread(
                owner.engine.chat.completions.create, **kwargs
            )
            if inspect.isawaitable(result):
                result = await result
            return _completion(result, body)

        if cached:
            if not streaming:
                owner.busy = False
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
            try:
                while not task.done():
                    await check()
                    await asyncio.sleep(0.02)
                await check()
                result = await task
                owner.result, success = result, True
                return JSONResponse(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise HTTPException(
                    502, "Native generation failed or disconnected"
                ) from None
            finally:
                await asyncio.shield(owners.finish(key, owner, success))

        stream_state = {"success": False, "cleaned": False}

        async def cleanup():
            if not stream_state["cleaned"]:
                stream_state["cleaned"] = True
                await asyncio.shield(owners.finish(key, owner, stream_state["success"]))

        async def stream():
            streamed = ""
            stream_id = owner.result["id"] if cached else "chatcmpl-" + uuid.uuid4().hex
            created = owner.result["created"] if cached else int(time.time())

            def chunk(delta=None, finish=None, usage=None):
                return {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body["model"],
                    "choices": []
                    if usage is not None
                    else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                    "usage": usage,
                }

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
                yield _sse(chunk(finish=result["choices"][0]["finish_reason"]))
                if (body.get("stream_options") or {}).get("include_usage") and result[
                    "usage"
                ] is not None:
                    yield _sse(chunk(usage=result["usage"]))
                yield _sse("[DONE]")
                if not cached:
                    # Keep the wire ID stable for an exact replay.
                    result["id"], result["created"] = stream_id, created
                    owner.result = result
                stream_state["success"] = True
            except asyncio.CancelledError:
                raise
            except Exception:
                yield _sse(
                    {
                        "error": {
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
