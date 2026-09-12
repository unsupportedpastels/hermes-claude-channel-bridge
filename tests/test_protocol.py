"""Offline regression tests; no native session or model calls."""

import copy
import json
import unittest

from claude_native_bridge.protocol import (
    HistoryTracker,
    ProtocolError,
    UnsupportedContent,
)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "system", "content": " system\n"},
            {"role": "developer", "content": "developer rules"},
            {"role": "user", "content": [{"type": "text", "text": " hello "}]},
        ]
        self.assistant = {"role": "assistant", "content": "long final " * 1000}
        self.tools = [
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ]
        self.tracker = HistoryTracker()

    def test_bootstrap_contains_unchanged_canonical_data(self):
        prepared = self.tracker.prepare(self.messages, self.tools, "auto")
        self.assertTrue(prepared["reset"])
        self.assertEqual(
            json.loads(prepared["content"]),
            {
                "operation": "bootstrap",
                "messages": self.messages,
                "tools": self.tools,
                "tool_choice": "auto",
            },
        )

    def test_hermes_empty_tool_assistant_content_matches_null_response(self):
        tracker = HistoryTracker()
        initial = [{"role": "user", "content": "test"}]
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "fixture", "arguments": "{}"},
                }
            ],
        }
        tracker.commit(initial, None, None, assistant)
        wire = {
            "role": "assistant",
            "content": "",
            "tool_calls": assistant["tool_calls"],
        }
        result = {"role": "tool", "tool_call_id": "call_x", "content": "actual result"}
        prepared = tracker.prepare(initial + [wire, result], None)
        self.assertFalse(prepared["reset"])
        self.assertEqual(json.loads(prepared["content"])["messages"], [result])

    def test_append_only_omits_previously_returned_long_final(self):
        self.tracker.commit(self.messages, self.tools, None, self.assistant)
        recorded = dict(
            self.assistant,
            reasoning_content="provider private metadata",
            tool_calls=None,
        )
        following = self.messages + [recorded, {"role": "user", "content": "next"}]
        prepared = self.tracker.prepare(following, self.tools)
        self.assertFalse(prepared["reset"])
        self.assertEqual(
            json.loads(prepared["content"]),
            {
                "operation": "continue",
                "messages": [{"role": "user", "content": "next"}],
            },
        )
        self.assertNotIn(self.assistant["content"], prepared["content"])

    def test_prepare_without_successful_commit_does_not_advance(self):
        self.tracker.prepare(self.messages, self.tools)
        self.assertTrue(self.tracker.prepare(self.messages, self.tools)["reset"])

    def test_changed_history_tools_choice_and_assistant_reset(self):
        self.tracker.commit(self.messages, self.tools, None, self.assistant)
        following = self.messages + [
            self.assistant,
            {"role": "user", "content": "next"},
        ]
        changed_history = copy.deepcopy(following)
        changed_history[0]["content"] = "changed system"
        changed_assistant = copy.deepcopy(following)
        changed_assistant[3]["content"] = "different answer"
        changed_tools = copy.deepcopy(self.tools)
        changed_tools[0]["function"]["description"] = "new semantics"
        for messages, tools, choice in [
            (changed_history, self.tools, None),
            (changed_assistant, self.tools, None),
            (following, changed_tools, None),
            (following, self.tools, "none"),
            (self.messages, self.tools, None),
        ]:
            with self.subTest(messages=messages[:1], choice=choice):
                prepared = self.tracker.prepare(messages, tools, choice)
                self.assertTrue(prepared["reset"])
                self.assertEqual(json.loads(prepared["content"])["messages"], messages)

    def test_separate_trackers_and_explicit_reset(self):
        self.tracker.commit(self.messages, None, None, self.assistant)
        following = self.messages + [self.assistant]
        self.assertFalse(self.tracker.prepare(following, None)["reset"])
        self.assertTrue(HistoryTracker().prepare(following, None)["reset"])
        self.tracker.reset()
        self.assertTrue(self.tracker.prepare(following, None)["reset"])

    def test_caller_objects_remain_unchanged_and_commit_is_a_snapshot(self):
        original = copy.deepcopy((self.messages, self.tools, self.assistant))
        self.tracker.prepare(self.messages, self.tools)
        self.tracker.commit(self.messages, self.tools, None, self.assistant)
        self.assertEqual((self.messages, self.tools, self.assistant), original)
        self.messages[0]["content"] = "mutation"
        self.tools[0]["function"]["name"] = "mutation"
        self.assistant["content"] = "mutation"
        messages, tools, assistant = original
        self.assertFalse(self.tracker.prepare(messages + [assistant], tools)["reset"])

    def test_optional_assistant_nulls_are_not_history_divergence(self):
        self.tracker.commit(self.messages, None, None, self.assistant)
        recorded = dict(
            self.assistant, refusal=None, audio=None, function_call=None, parsed=None
        )
        self.assertFalse(
            self.tracker.prepare(self.messages + [recorded], None)["reset"]
        )

    def test_tool_call_history_preserves_ids_names_arguments_and_tool_text(self):
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-hermes-1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"a":1,"b":" exact "}',
                    },
                }
            ],
        }
        self.tracker.commit(self.messages, self.tools, None, assistant)
        equivalent = copy.deepcopy(assistant)
        equivalent["tool_calls"][0]["index"] = 0
        equivalent["tool_calls"][0]["function"]["arguments"] = (
            '{ "b": " exact ", "a": 1 }'
        )
        tool = {
            "role": "tool",
            "tool_call_id": "call-hermes-1",
            "content": " exact\n\t result ",
        }
        following = self.messages + [equivalent, tool]
        prepared = self.tracker.prepare(following, self.tools)
        self.assertFalse(prepared["reset"])
        self.assertEqual(json.loads(prepared["content"])["messages"], [tool])
        for field, value in [
            ("id", "different-id"),
            ("name", "other"),
            ("arguments", '{"a":2,"b":" exact "}'),
        ]:
            changed = copy.deepcopy(following)
            call = changed[3]["tool_calls"][0]
            (call if field == "id" else call["function"])[field] = value
            with self.subTest(field=field):
                self.assertTrue(self.tracker.prepare(changed, self.tools)["reset"])

    def test_nonassistant_bookkeeping_and_whitespace_remain_significant(self):
        messages = self.messages + [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "result",
                "id": "keep",
            }
        ]
        self.tracker.commit(messages, None, None, self.assistant)
        for index, key, value in [
            (0, "content", "system"),
            (1, "id", "new"),
            (2, "reasoning_content", "not assistant metadata"),
            (3, "id", "changed"),
        ]:
            following = copy.deepcopy(messages + [self.assistant])
            following[index][key] = value
            with self.subTest(index=index):
                self.assertTrue(self.tracker.prepare(following, None)["reset"])

    def test_multiple_commits_do_not_repeat_old_messages(self):
        first = self.messages
        self.tracker.commit(first, None, None, self.assistant)
        second = first + [self.assistant, {"role": "user", "content": "second"}]
        answer = {"role": "assistant", "content": "second answer"}
        self.tracker.commit(second, None, None, answer)
        third = second + [answer, {"role": "user", "content": "third"}]
        frame = json.loads(self.tracker.prepare(third, None)["content"])
        self.assertEqual(frame, {"operation": "continue", "messages": [third[-1]]})

    def test_failed_commit_does_not_replace_successful_baseline(self):
        self.tracker.commit(self.messages, None, None, self.assistant)
        with self.assertRaises(ProtocolError):
            self.tracker.commit(
                self.messages, None, None, {"role": "user", "content": "wrong role"}
            )
        following = self.messages + [self.assistant]
        self.assertFalse(self.tracker.prepare(following, None)["reset"])
        following.append({"role": "system", "content": "new rules"})
        self.assertTrue(self.tracker.prepare(following, None)["reset"])

    def test_images_and_unknown_content_blocks_rejected(self):
        for block in [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            {"type": "audio", "data": "opaque"},
            {"text": "ambiguous"},
        ]:
            with self.subTest(block=block), self.assertRaises(UnsupportedContent):
                self.tracker.prepare([{"role": "user", "content": [block]}], None)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        from claude_native_bridge import protocol

        self.protocol = protocol
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        self.schema_tools = copy.deepcopy(self.tools)
        if protocol._jsonschema is None:
            self.tools[0]["function"].pop("parameters")
        self.final = {"request_id": "req-1", "kind": "final", "text": "answer"}
        self.calls = {
            "request_id": "req-1",
            "kind": "tool_calls",
            "tool_calls": [
                {"name": "lookup", "arguments": {"query": "exact text", "count": 2}},
            ],
        }

    def build(self, decision, **kwargs):
        return self.protocol.build_completion(
            decision, "req-1", "native-model", **kwargs
        )

    def test_final_shape_and_no_fabricated_usage(self):
        from unittest.mock import patch

        with patch.object(self.protocol.time, "time", return_value=1700000000.9):
            result = self.build(self.final)
        self.assertEqual(result.object, "chat.completion")
        self.assertTrue(result.id.startswith("chatcmpl-"))
        self.assertEqual(result.created, 1700000000)
        self.assertEqual(result.model, "native-model")
        self.assertIsNone(result.usage)
        self.assertEqual(len(result.choices), 1)
        self.assertEqual(result.choices[0].index, 0)
        self.assertEqual(result.choices[0].finish_reason, "stop")
        self.assertEqual(result.choices[0].message.role, "assistant")
        self.assertEqual(result.choices[0].message.content, "answer")
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertNotEqual(result.id, self.build(self.final).id)

    def test_tool_calls_are_nested_and_ids_unique(self):
        decision = copy.deepcopy(self.calls)
        decision["tool_calls"] *= 2
        result = self.build(decision, tools=self.tools)
        self.assertEqual(result.choices[0].finish_reason, "tool_calls")
        message = result.choices[0].message
        self.assertIsNone(message.content)
        calls = message.tool_calls
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call.id.startswith("call_"))
            self.assertEqual(call.type, "function")
            self.assertEqual(call.function.name, "lookup")
            self.assertEqual(
                json.loads(call.function.arguments), {"query": "exact text", "count": 2}
            )
        more = self.build(self.calls, tools=self.tools).choices[0].message.tool_calls
        self.assertEqual(len({call.id for call in calls + more}), 3)

    def test_decision_requires_exact_correlation(self):
        for request_id in ["req-2", " req-1", "req-1 ", 1, None, "", "x" * 257]:
            with self.subTest(request_id=request_id), self.assertRaises(ProtocolError):
                self.build(dict(self.final, request_id=request_id))

    def test_mutually_exclusive_strict_shapes(self):
        decisions = [
            None,
            [],
            {},
            dict(self.final, tool_calls=[]),
            dict(self.calls, text="also final"),
            dict(self.final, text=None),
            dict(self.final, kind="other"),
            dict(self.final, unexpected=True),
            {"request_id": "req-1", "kind": "final"},
            dict(self.calls, tool_calls=[]),
            dict(self.calls, tool_calls="bad"),
            dict(self.calls, tool_calls=self.calls["tool_calls"] * 17),
        ]
        for decision in decisions:
            with self.subTest(decision=decision), self.assertRaises(ProtocolError):
                self.build(decision, tools=self.tools)

    def test_poll_sequence_is_validated_transport_metadata(self):
        self.assertEqual(
            self.build(dict(self.final, sequence=1)).choices[0].message.content,
            "answer",
        )
        for value in [True, 0, -1, "1", None]:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                self.build(dict(self.final, sequence=value))

    def test_unknown_native_tool_and_scalar_arguments_rejected(self):
        calls = [
            {"name": "Bash", "arguments": {}},
            {"name": "lookup", "arguments": {}, "id": "native-id"},
        ]
        calls += [
            {"name": "lookup", "arguments": value}
            for value in [None, [], "{}", 1, True]
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ProtocolError):
                self.build(dict(self.calls, tool_calls=[call]), tools=self.tools)
        with self.assertRaises(ProtocolError):
            self.build(self.calls)

    def test_tool_choice_gating(self):
        named = {"type": "function", "function": {"name": "lookup"}}
        for choice in [None, "auto", "none"]:
            self.build(self.final, tools=self.tools, tool_choice=choice)
        for choice in [None, "auto", "required", named]:
            self.build(self.calls, tools=self.tools, tool_choice=choice)
        for choice in ["required", named]:
            with self.subTest(choice=choice), self.assertRaises(ProtocolError):
                self.build(self.final, tools=self.tools, tool_choice=choice)
        with self.assertRaises(ProtocolError):
            self.build(self.calls, tools=self.tools, tool_choice="none")
        for choice in [
            "nonsense",
            {},
            {"type": "function", "function": {"name": "unknown"}},
        ]:
            with self.subTest(choice=choice), self.assertRaises(ProtocolError):
                self.build(self.final, tools=self.tools, tool_choice=choice)
        two_tools = self.tools + [{"type": "function", "function": {"name": "other"}}]
        with self.assertRaises(ProtocolError):
            self.build(
                dict(self.calls, tool_calls=[{"name": "other", "arguments": {}}]),
                tools=two_tools,
                tool_choice=named,
            )

    def test_jsonschema_required_properties_types_and_additional_properties(self):
        if self.protocol._jsonschema is None:
            self.skipTest(
                "jsonschema not installed; fail-closed behavior tested separately"
            )
        self.build(self.calls, tools=self.schema_tools)
        for arguments in [
            {},
            {"query": 42},
            {"query": "q", "extra": 1},
            {"query": "q", "count": True},
            {"query": "q", "count": "2"},
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ProtocolError):
                self.build(
                    dict(
                        self.calls,
                        tool_calls=[{"name": "lookup", "arguments": arguments}],
                    ),
                    tools=self.tools,
                )

    def test_missing_jsonschema_fails_closed_for_declared_schema(self):
        from unittest.mock import patch

        with patch.object(self.protocol, "_jsonschema", None):
            with self.assertRaisesRegex(ProtocolError, "jsonschema"):
                self.build(self.calls, tools=self.schema_tools)
            tools = [{"type": "function", "function": {"name": "lookup"}}]
            self.build(self.calls, tools=tools)
            self.build(self.final)

    def test_invalid_and_remote_reference_schemas_rejected(self):
        for schema in [
            {"type": "made-up"},
            {"$ref": "https://example.invalid/schema.json"},
            {"properties": {"query": {"$ref": "file:///etc/passwd"}}},
        ]:
            tools = copy.deepcopy(self.tools)
            tools[0]["function"]["parameters"] = schema
            with self.subTest(schema=schema), self.assertRaises(ProtocolError):
                self.build(self.calls, tools=tools)

    def test_completion_does_not_mutate_inputs(self):
        original = copy.deepcopy((self.calls, self.tools))
        result = self.build(self.calls, tools=self.tools)
        self.assertEqual((self.calls, self.tools), original)
        result.choices[0].message.tool_calls[0].function.name = "changed"
        self.assertEqual((self.calls, self.tools), original)


if __name__ == "__main__":
    unittest.main()
