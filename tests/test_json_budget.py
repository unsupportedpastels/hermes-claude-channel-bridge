"""Offline boundary tests for model-proposed JSON resource budgets."""

import json

import pytest

from claude_native_bridge.protocol import (
    JSON_MAX_DEPTH,
    JSON_MAX_NODES,
    TOOL_PROPOSAL_MAX_BYTES,
    HistoryTracker,
    ProtocolError,
    build_completion,
)


def _decision(arguments):
    return {
        "request_id": "r",
        "kind": "tool_calls",
        "tool_calls": [{"name": "tool", "arguments": arguments}],
    }


def _tools(schema=None):
    function = {"name": "tool"}
    if schema is not None:
        function["parameters"] = schema
    return [{"type": "function", "function": function}]


def _build(arguments, schema=None):
    return build_completion(_decision(arguments), "r", "model", tools=_tools(schema))


def _depth(value):
    if isinstance(value, dict):
        return max((_depth(item) for item in value.values()), default=-1) + 1
    if isinstance(value, list):
        return max((_depth(item) for item in value), default=-1) + 1
    return 0


def _nodes(value):
    if isinstance(value, dict):
        return 1 + sum(_nodes(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_nodes(item) for item in value)
    return 1


def _encoded_bytes(value):
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def test_utf8_tool_proposal_byte_budget_accepts_exact_boundary_and_rejects_next_codepoint():
    empty = _decision({"payload": ""})
    remaining = TOOL_PROPOSAL_MAX_BYTES - _encoded_bytes(empty)
    payload = "é" * (remaining // 2) + "x" * (remaining % 2)
    exact = _decision({"payload": payload})
    assert _encoded_bytes(exact) == TOOL_PROPOSAL_MAX_BYTES
    build_completion(exact, "r", "model", tools=_tools())

    exact["tool_calls"][0]["arguments"]["payload"] += "é"
    with pytest.raises(ProtocolError, match="too large"):
        build_completion(exact, "r", "model", tools=_tools())


def test_tool_proposal_depth_budget_accepts_boundary_and_rejects_one_deeper():
    arguments = {}
    while _depth(_decision(arguments)) < JSON_MAX_DEPTH:
        arguments = {"nested": arguments}
    assert _depth(_decision(arguments)) == JSON_MAX_DEPTH
    _build(arguments)

    with pytest.raises(ProtocolError, match="depth"):
        _build({"nested": arguments})


def test_tool_proposal_node_budget_accepts_boundary_and_rejects_one_more():
    baseline = _decision({"items": []})
    item_count = JSON_MAX_NODES - _nodes(baseline)
    exact = _decision({"items": [0] * item_count})
    assert _nodes(exact) == JSON_MAX_NODES
    build_completion(exact, "r", "model", tools=_tools())

    exact["tool_calls"][0]["arguments"]["items"].append(0)
    with pytest.raises(ProtocolError, match="node budget"):
        build_completion(exact, "r", "model", tools=_tools())


def test_nonfinite_and_cyclic_proposals_are_rejected_as_protocol_errors():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ProtocolError, match="Invalid JSON"):
            _build({"value": value})

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ProtocolError, match="cycle"):
        _build(cyclic)


def test_budget_runs_before_jsonschema_and_remote_refs_still_fail_closed():
    too_deep = {}
    while _depth(_decision(too_deep)) <= JSON_MAX_DEPTH:
        too_deep = {"nested": too_deep}
    remote_schema = {"$ref": "https://example.invalid/schema.json"}
    with pytest.raises(ProtocolError, match="depth"):
        _build(too_deep, remote_schema)

    with pytest.raises(ProtocolError, match="Only local JSON Schema references"):
        _build({}, remote_schema)


def test_small_proposal_budget_does_not_cap_large_canonical_history():
    content = "é" * TOOL_PROPOSAL_MAX_BYTES
    prepared = HistoryTracker().prepare([{"role": "user", "content": content}], None)
    assert prepared["reset"] is True
    assert json.loads(prepared["content"])["messages"][0]["content"] == content
