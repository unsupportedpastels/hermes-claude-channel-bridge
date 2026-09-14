import json

import pytest

from claude_native_bridge import native_hooks
from claude_native_bridge.streaming import TextBatches

SESSION = "session"
PROMPT_A = "550e8400-e29b-41d4-a716-446655440000"
PROMPT_B = "650e8400-e29b-41d4-a716-446655440000"
TURN_A = "0c9e6a2f-7d41-4f4e-9a15-3f4f7c2b8d10"
TURN_B = "1c9e6a2f-7d41-4f4e-9a15-3f4f7c2b8d10"
TURN_C = "2c9e6a2f-7d41-4f4e-9a15-3f4f7c2b8d10"


def submitted(prompt_id):
    return {
        "session_id": SESSION,
        "prompt_id": prompt_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "opaque channel input",
    }


def display(prompt_id, turn_id, message_id, delta="text", final=True):
    return {
        "session_id": SESSION,
        "prompt_id": prompt_id,
        "hook_event_name": "MessageDisplay",
        "turn_id": turn_id,
        "message_id": message_id,
        "index": 0,
        "final": final,
        "delta": delta,
    }


def stop(prompt_id, text="answer"):
    return {
        "session_id": SESSION,
        "prompt_id": prompt_id,
        "hook_event_name": "Stop",
        "last_assistant_message": text,
    }


def retire(runtime, request_id, *, seal_prompt=True):
    return native_hooks.retire_request(
        runtime, SESSION, request_id, seal_prompt=seal_prompt
    )


def test_unseen_late_display_cannot_bind_after_next_request_opens(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "request-a")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_A))
    assert retire(tmp_path, "request-a") == PROMPT_A

    native_hooks.open_request(tmp_path, SESSION, "request-b")
    assert not native_hooks.capture(
        tmp_path, display(PROMPT_A, TURN_A, "previously-unseen-message-a")
    )
    assert not (tmp_path / "native-text.jsonl").exists()
    assert (tmp_path / "native-attribution-error").exists()


def test_late_stop_cannot_claim_request_after_its_display(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "request-a")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_A))
    assert retire(tmp_path, "request-a") == PROMPT_A

    native_hooks.open_request(tmp_path, SESSION, "request-b")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_B))
    assert native_hooks.capture(tmp_path, display(PROMPT_B, TURN_B, "message-b"))
    assert not native_hooks.capture(tmp_path, stop(PROMPT_A, "late answer-a"))
    assert not (tmp_path / "native-stop.json").exists()
    assert (tmp_path / "native-attribution-error").exists()


def test_stop_without_display_is_correlated_by_prompt_id(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "request-a")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_A))
    assert native_hooks.capture(tmp_path, stop(PROMPT_A, "tool-only final"))

    record = json.loads((tmp_path / "native-stop.json").read_text())
    assert record["prompt_id"] == PROMPT_A
    assert record["turn_id"] is None
    assert retire(tmp_path, "request-a") == PROMPT_A


def test_tool_requests_need_not_share_turn_id_or_emit_display(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "tool-1")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_A))
    assert native_hooks.capture(tmp_path, display(PROMPT_A, TURN_A, "message-1"))
    assert retire(tmp_path, "tool-1", seal_prompt=False) == PROMPT_A

    (tmp_path / "native-text.jsonl").unlink()
    native_hooks.open_request(
        tmp_path, SESSION, "tool-2", continued_prompt_id=PROMPT_A
    )
    assert retire(tmp_path, "tool-2", seal_prompt=False) == PROMPT_A

    native_hooks.open_request(
        tmp_path, SESSION, "tool-3", continued_prompt_id=PROMPT_A
    )
    assert native_hooks.capture(tmp_path, display(PROMPT_A, TURN_C, "message-3"))
    assert native_hooks.capture(tmp_path, stop(PROMPT_A, "final"))
    assert retire(tmp_path, "tool-3") == PROMPT_A


def test_request_cannot_retire_without_documented_prompt_boundary(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "request-a")
    with pytest.raises(ValueError, match="prompt boundary"):
        retire(tmp_path, "request-a")


def test_different_native_session_cannot_reuse_runtime_state(tmp_path):
    native_hooks.open_request(tmp_path, SESSION, "request-a")
    assert native_hooks.capture(tmp_path, submitted(PROMPT_A))
    assert retire(tmp_path, "request-a") == PROMPT_A

    with pytest.raises(ValueError, match="session changed"):
        native_hooks.open_request(tmp_path, "replacement-session", "request-b")


def test_journal_prompt_id_is_validated_independently(tmp_path):
    stream = TextBatches(SESSION, "request-b", expected_prompt_id=PROMPT_B)
    (tmp_path / "native-text.jsonl").write_text(
        json.dumps(
            {
                **display(PROMPT_A, TURN_A, "late-message-a"),
                "request_id": "request-b",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="prompt"):
        stream.drain(tmp_path)
