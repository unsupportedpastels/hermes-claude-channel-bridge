import json

import pytest

from claude_native_bridge.native import NativeSession, native_argv, native_hook_settings
from claude_native_bridge.native_hooks import (
    capture,
    open_request,
    retire_request,
    stopped_text,
)
from claude_native_bridge.settings import NativeBridgeError, Settings

SESSION = "native-session"
REQUEST = "hermes-request"
REQUEST_PROMPT = "request-prompt"
NEXT_REQUEST = "next-hermes-request"
NEXT_PROMPT = "next-request-prompt"
COMPACT_PROMPT = "compact-prompt"


def event(name, *, prompt_id=COMPACT_PROMPT, trigger="manual", **extra):
    return {
        "session_id": SESSION,
        "prompt_id": prompt_id,
        "hook_event_name": name,
        "trigger": trigger,
        **extra,
    }


def active_request(root):
    open_request(root, SESSION, REQUEST)
    assert capture(
        root,
        {
            "session_id": SESSION,
            "prompt_id": REQUEST_PROMPT,
            "hook_event_name": "UserPromptSubmit",
        },
    )


def state(root):
    return json.loads((root / "native-compaction.json").read_text())


def test_hooks_register_both_compaction_events_without_enabling_slash_commands():
    settings = native_hook_settings("observe", "status")
    assert set(settings["hooks"]) == {
        "UserPromptSubmit",
        "Stop",
        "StopFailure",
        "MessageDisplay",
        "PreCompact",
        "PostCompact",
    }
    assert settings["hooks"]["PreCompact"] == settings["hooks"]["UserPromptSubmit"]
    assert settings["hooks"]["PostCompact"] == settings["hooks"]["UserPromptSubmit"]
    assert "--disable-slash-commands" in native_argv(
        "claude", SESSION, "/tmp/mcp.json", "claude-sonnet-5", "low"
    )


@pytest.mark.parametrize("summary", ["retained context", "x"])
def test_idle_manual_compaction_is_observed_without_rebinding_retired_prompt(
    tmp_path, summary
):
    active_request(tmp_path)
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    before = json.loads((tmp_path / "active-request.json").read_text())

    assert capture(tmp_path, event("PreCompact"))
    assert capture(tmp_path, event("PostCompact", compact_summary=summary))

    assert json.loads((tmp_path / "active-request.json").read_text()) == before
    assert state(tmp_path) == {
        "active_request": False,
        "error": None,
        "generation": 1,
        "prompt_id": COMPACT_PROMPT,
        "request_id": None,
        "session_id": SESSION,
        "status": "completed",
        "summary_bytes": len(summary.encode()),
        "trigger": "manual",
    }
    assert not (tmp_path / "native-attribution-error").exists()


def test_active_auto_compaction_allows_display_before_correlated_postcompact(tmp_path):
    active_request(tmp_path)
    before = json.loads((tmp_path / "active-request.json").read_text())

    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": REQUEST_PROMPT,
            "hook_event_name": "MessageDisplay",
            "turn_id": "turn-after-precompact",
            "message_id": "compaction-display",
            "index": 0,
            "final": True,
            "delta": "Compacting conversation…",
        },
    )
    assert state(tmp_path)["status"] == "compacting"
    assert state(tmp_path)["error"] is None
    assert not (tmp_path / "native-attribution-error").exists()

    assert capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="summary",
        ),
    )

    after = json.loads((tmp_path / "active-request.json").read_text())
    assert {
        key: after[key] for key in before if key not in ("turn_id", "wake_generation")
    } == {
        key: before[key]
        for key in before
        if key not in ("turn_id", "wake_generation")
    }
    assert after["turn_id"] == "turn-after-precompact"
    assert after["wake_generation"] == 3
    assert state(tmp_path)["request_id"] == REQUEST
    assert state(tmp_path)["status"] == "completed"
    assert not (tmp_path / "native-attribution-error").exists()


def test_postcompact_without_start_records_failure_and_cancels_active_session(tmp_path):
    active_request(tmp_path)

    assert not capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="summary",
        ),
    )
    assert state(tmp_path)["status"] == "failed"
    assert state(tmp_path)["error"] == "missing_start"
    assert (tmp_path / "native-attribution-error").exists()
    with pytest.raises(ValueError, match="attribution previously failed"):
        retire_request(tmp_path, SESSION, REQUEST)


def test_mismatched_postcompact_records_failure_and_cancels(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )

    assert not capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id="different-prompt",
            trigger="auto",
            compact_summary="summary",
        ),
    )
    assert state(tmp_path)["status"] == "failed"
    assert state(tmp_path)["error"] == "correlation_mismatch"
    assert (tmp_path / "native-attribution-error").exists()
    with pytest.raises(ValueError, match="attribution previously failed"):
        retire_request(tmp_path, SESSION, REQUEST)


def test_terminal_without_postcompact_is_observation_incomplete_not_native_failure(
    tmp_path,
):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )

    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": REQUEST_PROMPT,
            "hook_event_name": "Stop",
            "last_assistant_message": "native completed normally",
        },
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    assert state(tmp_path)["status"] == "compacting"
    assert state(tmp_path)["error"] == "missing_end"
    assert not (tmp_path / "native-attribution-error").exists()


def test_stopfailure_distinguishes_native_failure_from_incomplete_observation(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )

    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": REQUEST_PROMPT,
            "hook_event_name": "StopFailure",
            "error": "native_transport_failed",
            "last_assistant_message": "not a successful response",
        },
    )
    observed = state(tmp_path)
    assert observed["status"] == "compacting"
    assert observed["error"] == "missing_end"
    terminal = json.loads((tmp_path / "native-stop.json").read_text())
    assert terminal["event"] == "StopFailure"
    assert terminal["error"] == "native_transport_failed"
    with pytest.raises(ValueError, match="native_transport_failed"):
        stopped_text(terminal, REQUEST, SESSION, REQUEST_PROMPT)
    assert not (tmp_path / "native-attribution-error").exists()


def test_correlated_postcompact_can_complete_after_request_retirement(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    assert state(tmp_path)["status"] == "compacting"
    assert state(tmp_path)["error"] == "missing_end"

    assert capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="eventually observed",
        ),
    )
    assert state(tmp_path)["status"] == "completed"
    assert state(tmp_path)["error"] is None
    assert not (tmp_path / "native-attribution-error").exists()


def test_delayed_postcompact_completes_original_identity_after_next_request_opens(
    tmp_path,
):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": REQUEST_PROMPT,
            "hook_event_name": "Stop",
            "last_assistant_message": "first request completed",
        },
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT

    open_request(tmp_path, SESSION, NEXT_REQUEST)
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": NEXT_PROMPT,
            "hook_event_name": "UserPromptSubmit",
        },
    )
    assert capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="delayed first-request summary",
        ),
    )

    assert state(tmp_path) == {
        "active_request": True,
        "error": None,
        "generation": 1,
        "prompt_id": REQUEST_PROMPT,
        "request_id": REQUEST,
        "session_id": SESSION,
        "status": "completed",
        "summary_bytes": len(b"delayed first-request summary"),
        "trigger": "auto",
    }
    current = json.loads((tmp_path / "active-request.json").read_text())
    assert current["request_id"] == NEXT_REQUEST
    assert current["prompt_id"] == NEXT_PROMPT
    assert current["open"] is True
    assert not (tmp_path / "native-attribution-error").exists()
    assert retire_request(tmp_path, SESSION, NEXT_REQUEST) == NEXT_PROMPT


def test_delayed_postcompact_failure_stays_owned_by_original_request(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    open_request(tmp_path, SESSION, NEXT_REQUEST)
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": NEXT_PROMPT,
            "hook_event_name": "UserPromptSubmit",
        },
    )

    assert not capture(
        tmp_path,
        event("PostCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert state(tmp_path)["status"] == "failed"
    assert state(tmp_path)["error"] == "missing_summary"
    assert state(tmp_path)["request_id"] == REQUEST
    assert state(tmp_path)["active_request"] is True
    assert not (tmp_path / "native-attribution-error").exists()
    assert retire_request(tmp_path, SESSION, NEXT_REQUEST) == NEXT_PROMPT


def test_unknown_old_postcompact_is_rejected_without_poisoning_next_request(tmp_path):
    active_request(tmp_path)
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    open_request(tmp_path, SESSION, NEXT_REQUEST)
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": NEXT_PROMPT,
            "hook_event_name": "UserPromptSubmit",
        },
    )

    assert not capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="unknown old summary",
        ),
    )
    assert state(tmp_path) == {
        "active_request": False,
        "error": "missing_start",
        "generation": 1,
        "prompt_id": REQUEST_PROMPT,
        "request_id": None,
        "session_id": SESSION,
        "status": "failed",
        "summary_bytes": None,
        "trigger": "auto",
    }
    assert not (tmp_path / "native-attribution-error").exists()
    assert retire_request(tmp_path, SESSION, NEXT_REQUEST) == NEXT_PROMPT


def test_unknown_old_postcompact_does_not_discard_retained_pending_identity(tmp_path):
    open_request(tmp_path, SESSION, "older-request")
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": "older-prompt",
            "hook_event_name": "UserPromptSubmit",
        },
    )
    assert retire_request(tmp_path, SESSION, "older-request") == "older-prompt"

    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT
    open_request(tmp_path, SESSION, NEXT_REQUEST)
    assert capture(
        tmp_path,
        {
            "session_id": SESSION,
            "prompt_id": NEXT_PROMPT,
            "hook_event_name": "UserPromptSubmit",
        },
    )

    assert not capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id="older-prompt",
            trigger="auto",
            compact_summary="uncorrelated older summary",
        ),
    )
    assert state(tmp_path)["error"] == "correlation_mismatch"
    assert not (tmp_path / "native-attribution-error").exists()

    assert capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="retained pending summary",
        ),
    )
    assert state(tmp_path)["status"] == "completed"
    assert state(tmp_path)["request_id"] == REQUEST
    assert not (tmp_path / "native-attribution-error").exists()
    assert retire_request(tmp_path, SESSION, NEXT_REQUEST) == NEXT_PROMPT


def test_native_polling_treats_incomplete_observation_as_unknown_not_failure(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert retire_request(tmp_path, SESSION, REQUEST) == REQUEST_PROMPT

    session = NativeSession(
        Settings(), tmp_path, "claude-sonnet-5", "low", http_client=object()
    )
    session.runtime = tmp_path
    session.session_id = SESSION
    assert session._check_compaction() is None
    observed = session.last_compaction
    assert observed is not None
    assert observed["status"] == "compacting"
    assert observed["error"] == "missing_end"

    broken = state(tmp_path)
    broken.update(status="failed", error="correlation_mismatch")
    (tmp_path / "native-compaction.json").write_text(json.dumps(broken))
    with pytest.raises(NativeBridgeError, match="observation failed"):
        session._check_compaction()


def test_compaction_generation_is_bounded_per_native_session(tmp_path):
    active_request(tmp_path)
    (tmp_path / "native-compaction.json").write_text(
        json.dumps(
            {
                "active_request": True,
                "error": None,
                "generation": 16_384,
                "prompt_id": REQUEST_PROMPT,
                "request_id": REQUEST,
                "session_id": SESSION,
                "status": "completed",
                "summary_bytes": 1,
                "trigger": "auto",
            }
        )
    )

    assert not capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert state(tmp_path)["generation"] == 16_384
    assert state(tmp_path)["status"] == "failed"
    assert state(tmp_path)["error"] == "limit_exceeded"


def test_last_compaction_is_a_safe_copy_without_native_payload_content(tmp_path):
    active_request(tmp_path)
    assert capture(
        tmp_path,
        event("PreCompact", prompt_id=REQUEST_PROMPT, trigger="auto"),
    )
    assert capture(
        tmp_path,
        event(
            "PostCompact",
            prompt_id=REQUEST_PROMPT,
            trigger="auto",
            compact_summary="private native summary",
            transcript_path="/private/transcript",
            custom_instructions="private instructions",
        ),
    )
    session = NativeSession(
        Settings(), tmp_path, "claude-sonnet-5", "low", http_client=object()
    )
    session.runtime = tmp_path
    session.session_id = SESSION

    observed = session.last_compaction
    assert observed == {
        "status": "completed",
        "trigger": "auto",
        "request_id": REQUEST,
        "active_request": True,
        "generation": 1,
        "summary_bytes": len(b"private native summary"),
        "error": None,
    }
    observed["status"] = "mutated"
    assert session.last_compaction["status"] == "completed"
    assert "private" not in json.dumps(session.last_compaction)
    (tmp_path / "native-compaction.json").unlink()
    cached = session.last_compaction
    assert cached is not None
    assert cached["status"] == "completed"
