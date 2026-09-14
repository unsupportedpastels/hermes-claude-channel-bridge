"""Canonical history frames and validated, non-executing decision conversion.

Frames carry roles as JSON data, not native role-equivalent messages. A tracker
belongs to one native session. Call commit only after a response was validated
and successfully delivered; prepare never advances the committed baseline.
"""

from __future__ import annotations

import copy
import json
import math
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

try:
    import jsonschema as _jsonschema
except ImportError:
    _jsonschema = None


class ProtocolError(ValueError):
    """The request or native decision violates the bridge protocol."""


class UnsupportedContent(ProtocolError):
    """Content cannot be represented faithfully by this text-only bridge."""


# These proposal-only limits are identical to channel/protocol.mjs. Depth is
# root-zero and nodes count each container and scalar value, but not object keys.
# Canonical history remains outside this deliberately narrow model-proposal
# budget and is bounded/paged by the separate transport machinery.
TOOL_PROPOSAL_MAX_BYTES = 256 * 1024
JSON_MAX_DEPTH = 32
JSON_MAX_NODES = 10_000


def _check_json(
    value: Any,
    *,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
) -> None:
    """Iteratively reject invalid JSON, cycles, and optional tree budgets."""

    active: set[int] = set()
    stack: list[tuple[str, Any, int, int]] = [("value", value, 0, 0)]
    nodes = 0
    encoded_bytes = 0
    while stack:
        operation, item, depth, index = stack.pop()
        if operation == "leave":
            active.remove(id(item))
            continue
        if operation in ("dict_items", "list_items"):
            try:
                child = next(item)
            except StopIteration:
                continue
            stack.append((operation, item, depth, index + 1))
            if operation == "dict_items":
                key, child = child
                if not isinstance(key, str):
                    raise ProtocolError("JSON object keys must be strings")
                if max_bytes is not None:
                    encoded_bytes += (1 if index else 0) + 1
                    encoded_bytes += len(
                        json.dumps(key, ensure_ascii=False).encode("utf-8")
                    )
            elif max_bytes is not None and index:
                encoded_bytes += 1
            stack.append(("value", child, depth, 0))
            if max_bytes is not None and encoded_bytes > max_bytes:
                raise ProtocolError("Tool proposal JSON is too large")
            continue
        nodes += 1
        if max_nodes is not None and nodes > max_nodes:
            raise ProtocolError("JSON data exceeds the tool proposal node budget")
        if max_depth is not None and depth > max_depth:
            raise ProtocolError("JSON data exceeds the tool proposal depth budget")
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in active:
                raise ProtocolError("Invalid JSON data: container cycle detected")
            active.add(identity)
            stack.append(("leave", item, depth, 0))
            if max_bytes is not None:
                encoded_bytes += 2
            if isinstance(item, dict):
                stack.append(("dict_items", iter(item.items()), depth + 1, 0))
            else:
                stack.append(("list_items", iter(item), depth + 1, 0))
        elif isinstance(item, float) and not math.isfinite(item):
            raise ProtocolError("Invalid JSON data: non-finite number")
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ProtocolError("Only JSON-compatible values are supported")
        elif max_bytes is not None:
            encoded_bytes += len(
                json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        if max_bytes is not None and encoded_bytes > max_bytes:
            raise ProtocolError("Tool proposal JSON is too large")


def _json(
    value: Any,
    *,
    sort_keys: bool = False,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
) -> str:
    """Encode strict JSON, optionally enforcing pre-recursion resource budgets."""

    try:
        _check_json(
            value,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_bytes=max_bytes,
        )
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        )
        return encoded
    except ProtocolError:
        raise
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ProtocolError("Invalid JSON data") from exc


def _equal(left: Any, right: Any) -> bool:
    return _json(left, sort_keys=True) == _json(right, sort_keys=True)


def _messages(messages: list[dict]) -> list[dict]:
    if not isinstance(messages, list):
        raise ProtocolError("messages must be a list")
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError("Each message must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise UnsupportedContent("Unsupported message role")
        if any(
            message.get(key) is not None
            for key in ("audio", "images", "image_url", "attachments")
        ):
            raise UnsupportedContent(
                "Only text content and function tool calls are supported"
            )
        content = message.get("content")
        if content is None:
            if role != "assistant":
                raise UnsupportedContent("Non-assistant messages require text content")
        elif isinstance(content, list):
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                    or set(part) != {"type", "text"}
                ):
                    raise UnsupportedContent(
                        "Only explicit text content blocks are supported"
                    )
        elif not isinstance(content, str):
            raise UnsupportedContent(
                "Only strings or lists of text blocks are supported"
            )
        if message.get("function_call") is not None:
            raise UnsupportedContent("Legacy function_call messages are unsupported")
        calls = message.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list):
                raise ProtocolError(
                    "Only assistant messages can contain a tool_calls list"
                )
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or not isinstance(call.get("function"), dict)
                ):
                    raise ProtocolError("Malformed canonical tool call")
                function = call["function"]
                if (
                    not isinstance(function.get("name"), str)
                    or not function["name"]
                    or not isinstance(function.get("arguments"), (str, dict))
                ):
                    raise ProtocolError("Malformed canonical tool function")
        if role == "tool" and (
            not isinstance(message.get("tool_call_id"), str)
            or not message["tool_call_id"]
        ):
            raise ProtocolError("Tool results require tool_call_id")
    _json(messages)
    return copy.deepcopy(messages)


# Ignore only known assistant bookkeeping. Unknown fields remain significant;
# non-assistant messages are never projected or normalized.
_ASSISTANT_BOOKKEEPING = frozenset(
    {
        "reasoning_content",
        "reasoning",
        "reasoning_details",
        "provider_specific_fields",
        "usage",
        "finish_reason",
        "model",
        "created",
        "id",
        "object",
        "annotations",
    }
)


def _projection(messages: list[dict]) -> list[dict]:
    result = copy.deepcopy(messages)
    for message in result:
        if message["role"] != "assistant":
            continue
        for key in _ASSISTANT_BOOKKEEPING:
            message.pop(key, None)
        for key in ("refusal", "audio", "function_call", "parsed"):
            if message.get(key) is None:
                message.pop(key, None)
        message.setdefault("content", None)
        # Hermes' canonical wire uses an empty string for tool-only assistant
        # messages; a native structured decision has no textual content.
        if message.get("tool_calls") and message["content"] == "":
            message["content"] = None
        if not message.get("tool_calls"):
            message.pop("tool_calls", None)
        for call in message.get("tool_calls", []):
            call.pop("index", None)
            arguments = call["function"]["arguments"]
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                    if isinstance(parsed, dict):
                        _json(parsed)
                        call["function"]["arguments"] = parsed
                except (ValueError, RecursionError):
                    pass  # Preserve malformed historic argument strings exactly.
    return result


class HistoryTracker:
    """Per-session, commit-on-success append-only history tracker (not thread safe)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._history: list[dict] | None = None
        self._tools: list[dict] | None = None
        self._tool_choice: Any = None

    def prepare(
        self, messages: list[dict], tools: list[dict] | None, tool_choice: Any = None
    ) -> dict:
        canonical = _messages(messages)
        if tools is not None and (
            not isinstance(tools, list)
            or any(not isinstance(tool, dict) for tool in tools)
        ):
            raise ProtocolError("tools must be a list of objects or None")
        _json([tools, tool_choice])
        projected = _projection(canonical)
        reset = (
            self._history is None
            or not _equal(tools, self._tools)
            or not _equal(tool_choice, self._tool_choice)
        )
        if not reset:
            count = len(self._history)
            reset = (
                len(projected) < count
                or not _equal(projected[:count], self._history)
                or any(m["role"] in ("system", "developer") for m in canonical[count:])
            )
        if reset:
            frame = {
                "operation": "bootstrap",
                "messages": canonical,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        else:
            frame = {
                "operation": "continue",
                "messages": canonical[len(self._history) :],
            }
        return {"reset": reset, "content": _json(frame)}

    def commit(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: Any,
        assistant_message: dict,
    ) -> None:
        # Complete validation before replacing any committed state.
        self.prepare(messages, tools, tool_choice)
        assistant = _messages([assistant_message])[0]
        if assistant["role"] != "assistant":
            raise ProtocolError("commit requires an assistant message")
        history = _projection(_messages(messages) + [assistant])
        self._history = history
        self._tools = copy.deepcopy(tools)
        self._tool_choice = copy.deepcopy(tool_choice)


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256


def _tool_definitions(tools: list[dict] | None) -> dict[str, dict]:
    if tools is None:
        return {}
    if not isinstance(tools, list):
        raise ProtocolError("tools must be a list or None")
    _json(tools)
    definitions = {}
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or not isinstance(tool.get("function"), dict)
        ):
            raise ProtocolError("Only OpenAI function tool definitions are supported")
        function = tool["function"]
        name = function.get("name")
        if not _identifier(name) or name in definitions:
            raise ProtocolError("Tool names must be nonempty and unique")
        schema = function.get("parameters", {})
        if not isinstance(schema, (dict, bool)):
            raise ProtocolError(
                "Tool parameters must be a JSON Schema object or boolean"
            )
        definitions[name] = function
    return definitions


def _choice(tool_choice: Any, definitions: dict[str, dict]) -> tuple[str, str | None]:
    if tool_choice is None:
        return "auto", None
    if isinstance(tool_choice, str) and tool_choice in ("auto", "none", "required"):
        if tool_choice == "required" and not definitions:
            raise ProtocolError("tool_choice required needs available tools")
        return tool_choice, None
    if (
        isinstance(tool_choice, dict)
        and set(tool_choice) == {"type", "function"}
        and tool_choice["type"] == "function"
        and isinstance(tool_choice["function"], dict)
        and set(tool_choice["function"]) == {"name"}
    ):
        name = tool_choice["function"]["name"]
        if _identifier(name) and name in definitions:
            return "required", name
    raise ProtocolError("Invalid or unavailable tool_choice")


def _validate_arguments(arguments: dict, schema: dict | bool) -> None:
    """Delegate schemas to jsonschema; never implement a partial validator.

    Without jsonschema only unconstrained object arguments are accepted. Any
    declared constraints fail closed with an explicit dependency error. Remote
    references are disallowed so validation cannot fetch files or the network.
    """
    if schema == {} or schema is True:
        return
    if _jsonschema is None:
        raise ProtocolError(
            "jsonschema is required to validate declared tool parameter schemas"
        )

    def local_references(value: Any) -> None:
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                for key, item in current.items():
                    if key in ("$ref", "$dynamicRef", "$recursiveRef") and (
                        not isinstance(item, str) or not item.startswith("#")
                    ):
                        raise ProtocolError(
                            "Only local JSON Schema references are supported"
                        )
                    pending.append(item)
            elif isinstance(current, list):
                pending.extend(current)

    local_references(schema)
    try:
        validator_class = _jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        validator_class(schema).validate(arguments)
    except Exception as exc:
        # Do not interpolate arguments or schema values into a user-facing error.
        raise ProtocolError(
            "Tool arguments or parameter schema failed JSON Schema validation"
        ) from exc


def build_completion(
    decision: dict,
    request_id: str,
    model: str,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
) -> SimpleNamespace:
    """Validate a correlated channel decision and return an OpenAI-like object.

    Accepted decisions match channel/protocol.mjs:
      {"request_id": "...", "kind": "final", "text": "..."}
      {"request_id": "...", "kind": "tool_calls", "tool_calls": [
          {"name": "supplied_function", "arguments": {"key": "value"}}]}

    Optional positive integer `sequence` is transport metadata only; the caller
    must enforce monotonic delivery and acknowledgement. Tool batches contain
    1..16 calls. This function neither executes tools nor commits history.
    """
    if (
        not _identifier(request_id)
        or not isinstance(decision, dict)
        or not _identifier(decision.get("request_id"))
        or decision["request_id"] != request_id
    ):
        raise ProtocolError("Decision request_id does not exactly match the request")
    if not isinstance(model, str) or not model:
        raise ProtocolError("model must be a nonempty string")
    if decision.get("kind") == "tool_calls":
        _json(
            decision,
            max_depth=JSON_MAX_DEPTH,
            max_nodes=JSON_MAX_NODES,
            max_bytes=TOOL_PROPOSAL_MAX_BYTES,
        )
    else:
        _json(decision)
    fields = set(decision)
    if "sequence" in fields:
        sequence = decision["sequence"]
        if type(sequence) is not int or not 0 < sequence <= 9007199254740991:
            raise ProtocolError("Invalid decision sequence")
        fields.remove("sequence")
    definitions = _tool_definitions(tools)
    mode, specified = _choice(tool_choice, definitions)
    kind = decision.get("kind")
    if kind == "final":
        if fields != {"request_id", "kind", "text"} or not isinstance(
            decision["text"], str
        ):
            raise ProtocolError(
                "A final decision must contain only request_id, kind, and text"
            )
        if mode == "required":
            raise ProtocolError("tool_choice requires a tool call, not a final answer")
        message = SimpleNamespace(
            role="assistant", content=decision["text"], tool_calls=None
        )
        finish_reason = "stop"
    elif kind == "tool_calls":
        if fields != {"request_id", "kind", "tool_calls"}:
            raise ProtocolError(
                "A tool decision must contain only request_id, kind, and tool_calls"
            )
        calls = decision["tool_calls"]
        if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
            raise ProtocolError(
                "tool_calls must be a nonempty batch of at most 16 calls"
            )
        if mode == "none":
            raise ProtocolError("tool_choice none forbids tool calls")
        converted = []
        for call in calls:
            if (
                not isinstance(call, dict)
                or set(call) != {"name", "arguments"}
                or not _identifier(call["name"])
                or not isinstance(call["arguments"], dict)
            ):
                raise ProtocolError(
                    "Each tool call requires a name and an object of arguments"
                )
            name = call["name"]
            if name not in definitions:
                raise ProtocolError(
                    "Decision selected a tool absent from the supplied definitions"
                )
            if specified is not None and name != specified:
                raise ProtocolError("Decision violates the specified tool_choice")
            _validate_arguments(
                call["arguments"], definitions[name].get("parameters", {})
            )
            converted.append(
                SimpleNamespace(
                    id="call_" + uuid4().hex,
                    type="function",
                    function=SimpleNamespace(
                        name=name, arguments=_json(call["arguments"])
                    ),
                )
            )
        message = SimpleNamespace(role="assistant", content=None, tool_calls=converted)
        finish_reason = "tool_calls"
    else:
        raise ProtocolError("Decision kind must be final or tool_calls")
    return SimpleNamespace(
        id="chatcmpl-" + uuid4().hex,
        object="chat.completion",
        created=int(time.time()),
        model=model,
        usage=None,
        choices=[
            SimpleNamespace(index=0, message=message, finish_reason=finish_reason)
        ],
    )
