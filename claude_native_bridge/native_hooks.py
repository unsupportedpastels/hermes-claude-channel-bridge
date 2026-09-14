"""Record correlated native prompt/display/terminal hooks without inference."""

import json
import os
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PROMPTS = 16384
MAX_COMPACTIONS = 16384
MAX_WAKE_GENERATION = 65_536
MAX_CONTROL_BYTES = 16 * 1024
MAX_COMPACTION_ID_LENGTH = 4096
WAKE_TIMEOUT_SECONDS = 0.05
WAKE_EVENTS = frozenset(("MessageDisplay", "Stop", "StopFailure", "Usage"))
COMPACTION_TRIGGERS = frozenset(("auto", "manual"))
COMPACTION_STATUSES = frozenset(("compacting", "completed", "failed"))


def _private_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    os.replace(temporary, path)


def _mark_attribution_error(runtime):
    (runtime / "native-attribution-error").touch(mode=0o600)


@contextmanager
def _runtime_lock(runtime):
    """Serialize request ownership with command-hook processes on every OS."""
    lock = runtime / "native-hook.lock"
    deadline = time.monotonic() + 1
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                _mark_attribution_error(runtime)
                raise TimeoutError("Native hook attribution lock timed out")
            time.sleep(0.005)
    try:
        yield
    finally:
        lock.rmdir()


def _read_current(runtime):
    current = json.loads((runtime / "active-request.json").read_text())
    retired = current.get("retired_prompt_ids", [])
    if not isinstance(retired, list) or any(
        not isinstance(prompt, str) or not prompt for prompt in retired
    ):
        raise ValueError("Invalid native prompt state")
    return current, retired


def _read_compaction_locked(runtime):
    return _read_compaction_path_locked(runtime / "native-compaction.json")


def _read_pending_compaction_locked(runtime):
    return _read_compaction_path_locked(runtime / "native-compaction-pending.json")


def _read_compaction_path_locked(path):
    if not path.exists():
        return None
    value = _read_control_json(path)
    if (
        value.get("status") not in COMPACTION_STATUSES
        or value.get("trigger") not in COMPACTION_TRIGGERS
        or not isinstance(value.get("session_id"), str)
        or not value["session_id"]
        or len(value["session_id"]) > MAX_COMPACTION_ID_LENGTH
        or not isinstance(value.get("prompt_id"), str)
        or not value["prompt_id"]
        or len(value["prompt_id"]) > MAX_COMPACTION_ID_LENGTH
        or not (
            value.get("request_id") is None
            or (
                isinstance(value["request_id"], str)
                and 0 < len(value["request_id"]) <= MAX_COMPACTION_ID_LENGTH
            )
        )
        or type(value.get("active_request")) is not bool
        or type(value.get("generation")) is not int
        or not 1 <= value["generation"] <= MAX_COMPACTIONS
        or not (value.get("summary_bytes") is None or type(value["summary_bytes"]) is int)
        or not (value.get("error") is None or isinstance(value["error"], str))
    ):
        raise ValueError("Invalid native compaction state")
    return value


def _fail_compaction_locked(runtime, current, error, *, payload=None, owner=None):
    previous = _read_compaction_locked(runtime)
    payload = payload or {}
    owner = owner or {}
    generation = owner.get("generation")
    if type(generation) is not int:
        generation = previous["generation"] if previous is not None else 1
    prompt_id = payload.get("prompt_id") or owner.get("prompt_id")
    if not isinstance(prompt_id, str) or not 0 < len(prompt_id) <= MAX_COMPACTION_ID_LENGTH:
        prompt_id = current.get("prompt_id")
    if not isinstance(prompt_id, str) or not 0 < len(prompt_id) <= MAX_COMPACTION_ID_LENGTH:
        prompt_id = "unknown"
    retired = current.get("retired_prompt_ids", [])
    known_old_event = prompt_id in retired and prompt_id != current.get("prompt_id")
    owner_request_id = owner.get("request_id")
    owner_active = owner.get("active_request") is True
    if owner:
        poisons_current = (
            current.get("open") is True
            and owner_request_id == current.get("request_id")
        )
    else:
        poisons_current = current.get("open") is True and not known_old_event
        owner_request_id = current.get("request_id") if poisons_current else None
        owner_active = poisons_current
    record = {
        "session_id": owner.get("session_id", current.get("session_id")),
        "request_id": owner_request_id,
        "prompt_id": prompt_id,
        "trigger": payload.get("trigger")
        if payload.get("trigger") in COMPACTION_TRIGGERS
        else owner.get("trigger", (previous or {}).get("trigger", "auto")),
        "status": "failed",
        "active_request": owner_active,
        "generation": generation,
        "summary_bytes": None,
        "error": error,
    }
    _private_json(runtime / "native-compaction.json", record)
    if poisons_current:
        _mark_attribution_error(runtime)
    return record


def _mark_compaction_incomplete_locked(runtime):
    """Record a missing PostCompact as unknown, not as a native failure."""
    previous = _read_compaction_locked(runtime)
    pending = _read_pending_compaction_locked(runtime)
    record = previous
    if previous is not None and previous["status"] == "compacting":
        record = dict(previous)
        record["error"] = "missing_end"
        _private_json(runtime / "native-compaction.json", record)
    if pending is not None and pending["status"] == "compacting":
        pending = dict(pending)
        pending["error"] = "missing_end"
        _private_json(runtime / "native-compaction-pending.json", pending)
    return record


def compaction_state(runtime, session_id):
    """Return a validated projection with no native summary or path content."""
    runtime = Path(runtime)
    try:
        with _runtime_lock(runtime):
            value = _read_compaction_locked(runtime)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, TimeoutError):
        return None
    if value is None or value.get("session_id") != session_id:
        return None
    return {
        key: value.get(key)
        for key in (
            "status",
            "trigger",
            "request_id",
            "active_request",
            "generation",
            "summary_bytes",
            "error",
        )
    }


def _read_control_json(path):
    if path.stat().st_size > MAX_CONTROL_BYTES:
        raise ValueError("Oversized native wake control file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid native wake control file")
    return value


def notify_wake(runtime, wake, *, timeout=WAKE_TIMEOUT_SECONDS):
    """Best-effort authenticated loopback hint; journals remain authoritative."""
    runtime = Path(runtime)
    try:
        ready = _read_control_json(runtime / "ready.json")
        transport = _read_control_json(runtime / "transport.json")
        if set(ready) != {"port", "pid"} or set(transport) != {"token"}:
            return False
        port = ready.get("port")
        token = transport.get("token")
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(not 0x21 <= ord(char) <= 0x7E for char in token)
        ):
            return False
        body = json.dumps(wake, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_CONTROL_BYTES:
            return False
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/wake",
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and len(
                response.read(MAX_CONTROL_BYTES + 1)
            ) <= MAX_CONTROL_BYTES
    except (OSError, ValueError, TypeError):
        return False


def _next_wake_locked(runtime, current, event):
    generation = current.get("wake_generation", 0)
    if type(generation) is not int or not 0 <= generation < MAX_WAKE_GENERATION:
        _mark_attribution_error(runtime)
        raise ValueError("Native wake generation limit exceeded")
    prompt_id = current.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        _mark_attribution_error(runtime)
        raise ValueError("Native wake prompt is unavailable")
    generation += 1
    current["wake_generation"] = generation
    _private_json(runtime / "active-request.json", current)
    return {
        "request_id": current["request_id"],
        "prompt_id": prompt_id,
        "event": event,
        "generation": generation,
    }


def wake_current(runtime, event="Usage"):
    """Integration seam for a canonical producer such as the status-line hook."""
    runtime = Path(runtime)
    if event != "Usage":
        return False
    try:
        with _runtime_lock(runtime):
            current, _ = _read_current(runtime)
            if current.get("open") is not True:
                return False
            wake = _next_wake_locked(runtime, current, event)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, TimeoutError):
        return False
    return notify_wake(runtime, wake)


def open_request(runtime, session_id, request_id, continued_prompt_id=None):
    """Open one request window in this native session's dedicated runtime."""
    runtime = Path(runtime)
    with _runtime_lock(runtime):
        retired = []
        path = runtime / "active-request.json"
        if path.exists():
            previous, retired = _read_current(runtime)
            if previous.get("session_id") != session_id:
                _mark_attribution_error(runtime)
                raise ValueError("Native hook session changed in a dedicated runtime")
            if previous.get("open") is True:
                _mark_attribution_error(runtime)
                raise ValueError("Prior native hook request is still open")
            previous_prompt = previous.get("prompt_id")
            previous_unsealed = (
                isinstance(previous_prompt, str)
                and previous_prompt
                and previous_prompt not in retired
            )
            if previous_unsealed and continued_prompt_id != previous_prompt:
                _mark_attribution_error(runtime)
                raise ValueError("Unsealed native prompt was not continued")
            if not previous_unsealed and continued_prompt_id is not None:
                _mark_attribution_error(runtime)
                raise ValueError("No native prompt is available to continue")
            compaction = _read_compaction_locked(runtime)
            if compaction is not None and compaction["status"] == "compacting":
                _mark_compaction_incomplete_locked(runtime)
        if (runtime / "native-attribution-error").exists():
            raise ValueError("Native hook attribution previously failed")
        _private_json(
            path,
            {
                "session_id": session_id,
                "request_id": request_id,
                "prompt_id": continued_prompt_id,
                "turn_id": None,
                "retired_prompt_ids": retired,
                "open": True,
                "wake_generation": 0,
            },
        )


def retire_request(runtime, session_id, request_id, *, seal_prompt=True):
    """Close a request only after its documented prompt boundary was observed."""
    runtime = Path(runtime)
    with _runtime_lock(runtime):
        try:
            current, retired = _read_current(runtime)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _mark_attribution_error(runtime)
            raise ValueError("Native hook attribution state is unavailable") from None
        if (runtime / "native-attribution-error").exists():
            raise ValueError("Native hook attribution previously failed")
        if (
            current.get("session_id") != session_id
            or current.get("request_id") != request_id
            or current.get("open") is not True
        ):
            _mark_attribution_error(runtime)
            raise ValueError("Native hook attribution request changed unexpectedly")
        compaction = _read_compaction_locked(runtime)
        if (
            compaction is not None
            and compaction["status"] == "compacting"
            and compaction.get("request_id") == request_id
        ):
            _mark_compaction_incomplete_locked(runtime)
        prompt_id = current.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            _mark_attribution_error(runtime)
            raise ValueError("Native prompt boundary was not observed")
        if prompt_id in retired:
            _mark_attribution_error(runtime)
            raise ValueError("Native prompt was already retired")
        if seal_prompt:
            if len(retired) >= MAX_PROMPTS:
                _mark_attribution_error(runtime)
                raise ValueError("Native prompt attribution limit exceeded")
            retired.append(prompt_id)
        current.update(open=False, retired_prompt_ids=retired)
        _private_json(runtime / "active-request.json", current)
        return prompt_id


def _bind_prompt_locked(runtime, payload, current):
    prompt_id = payload.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        _mark_attribution_error(runtime)
        return False
    if prompt_id in current.get("retired_prompt_ids", []):
        _mark_attribution_error(runtime)
        return False
    expected = current.get("prompt_id")
    if expected is not None and prompt_id != expected:
        _mark_attribution_error(runtime)
        return False
    if expected is None:
        current["prompt_id"] = prompt_id
        _private_json(runtime / "active-request.json", current)
    return True


def _capture_display_locked(runtime, payload, current):
    if not _bind_prompt_locked(runtime, payload, current):
        return None
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        _mark_attribution_error(runtime)
        return None
    expected_turn = current.get("turn_id")
    if expected_turn is not None and turn_id != expected_turn:
        _mark_attribution_error(runtime)
        return None
    if expected_turn is None:
        current["turn_id"] = turn_id
        _private_json(runtime / "active-request.json", current)
    wake = _next_wake_locked(runtime, current, "MessageDisplay")
    record = {
        key: payload.get(key)
        for key in (
            "session_id",
            "prompt_id",
            "turn_id",
            "message_id",
            "index",
            "final",
            "delta",
        )
    }
    record["request_id"] = current["request_id"]
    record["generation"] = wake["generation"]
    raw = (json.dumps(record) + "\n").encode("utf-8")
    target = runtime / "native-text.jsonl"
    size = target.stat().st_size if target.exists() else 0
    if size + len(raw) > MAX_CAPTURE_BYTES:
        (runtime / "native-text-error").touch(mode=0o600)
        return None
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as output:
        output.write(raw)
    return wake


def _capture_compaction_locked(runtime, payload, current):
    event = payload.get("hook_event_name")
    trigger = payload.get("trigger")
    prompt_id = payload.get("prompt_id")
    active = current.get("open") is True
    if (
        trigger not in COMPACTION_TRIGGERS
        or not isinstance(prompt_id, str)
        or not 0 < len(prompt_id) <= MAX_COMPACTION_ID_LENGTH
    ):
        _fail_compaction_locked(runtime, current, "invalid_event", payload=payload)
        return None

    previous = _read_compaction_locked(runtime)
    pending = _read_pending_compaction_locked(runtime)
    if event == "PreCompact":
        if (
            pending is not None
            and pending["status"] == "compacting"
            and pending.get("error") != "missing_end"
        ):
            _fail_compaction_locked(runtime, current, "overlapping_start", payload=payload)
            return None
        if (active and trigger != "auto") or (not active and trigger != "manual"):
            _fail_compaction_locked(runtime, current, "invalid_context", payload=payload)
            return None
        if active and prompt_id != current.get("prompt_id"):
            _fail_compaction_locked(runtime, current, "correlation_mismatch", payload=payload)
            return None
        generation = 1 if previous is None else previous["generation"] + 1
        if generation > MAX_COMPACTIONS:
            _fail_compaction_locked(runtime, current, "limit_exceeded", payload=payload)
            return None
        record = {
            "session_id": current["session_id"],
            "request_id": current["request_id"] if active else None,
            "prompt_id": prompt_id,
            "trigger": trigger,
            "status": "compacting",
            "active_request": active,
            "generation": generation,
            "summary_bytes": None,
            "error": None,
        }
        _private_json(runtime / "native-compaction.json", record)
        _private_json(runtime / "native-compaction-pending.json", record)
    else:
        retained = pending
        if retained is None and previous is not None and previous["status"] == "compacting":
            retained = previous
        if retained is None:
            _fail_compaction_locked(runtime, current, "missing_start", payload=payload)
            return None
        if (
            retained["session_id"] != current.get("session_id")
            or retained["prompt_id"] != prompt_id
            or retained["trigger"] != trigger
        ):
            _fail_compaction_locked(runtime, current, "correlation_mismatch", payload=payload)
            return None
        summary = payload.get("compact_summary")
        if not isinstance(summary, str) or not summary:
            _fail_compaction_locked(
                runtime,
                current,
                "missing_summary",
                payload=payload,
                owner=retained,
            )
            (runtime / "native-compaction-pending.json").unlink(missing_ok=True)
            return None
        record = dict(retained)
        record.update(
            status="completed",
            summary_bytes=len(summary.encode("utf-8")),
            error=None,
        )
        _private_json(runtime / "native-compaction.json", record)
        (runtime / "native-compaction-pending.json").unlink(missing_ok=True)

    # The channel already accepts Usage wakes. Reuse that transport hint for
    # active automatic compaction; the journal above remains authoritative.
    same_active_request = active and record.get("request_id") == current.get("request_id")
    return _next_wake_locked(runtime, current, "Usage") if same_active_request else {}


def capture(runtime, payload):
    runtime = Path(runtime)
    wake = None
    try:
        with _runtime_lock(runtime):
            current, _ = _read_current(runtime)
            if payload.get("session_id") != current.get("session_id"):
                return False
            event = payload.get("hook_event_name")
            if (
                current.get("open", True) is not True
                and event not in ("PreCompact", "PostCompact")
            ):
                _mark_attribution_error(runtime)
                return False
            if event in ("PreCompact", "PostCompact"):
                wake = _capture_compaction_locked(runtime, payload, current)
            elif event == "UserPromptSubmit":
                return _bind_prompt_locked(runtime, payload, current)
            if event == "MessageDisplay":
                wake = _capture_display_locked(runtime, payload, current)
            elif event not in ("PreCompact", "PostCompact"):
                if event not in ("Stop", "StopFailure"):
                    return False
                compaction = _read_compaction_locked(runtime)
                if compaction is not None and compaction["status"] == "compacting":
                    _mark_compaction_incomplete_locked(runtime)
                if not _bind_prompt_locked(runtime, payload, current):
                    return False
                wake = _next_wake_locked(runtime, current, event)
                record = {
                    "request_id": current["request_id"],
                    "session_id": current["session_id"],
                    "prompt_id": current["prompt_id"],
                    "turn_id": current.get("turn_id"),
                    "event": event,
                    "text": payload.get("last_assistant_message"),
                    "error": payload.get("error"),
                    "background_pending": bool(
                        payload.get("background_tasks") or payload.get("session_crons")
                    ),
                    "generation": wake["generation"],
                }
                _private_json(runtime / "native-stop.json", record)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, TimeoutError):
        _mark_attribution_error(runtime)
        return False
    except Exception:
        _mark_attribution_error(runtime)
        raise
    if wake is None:
        return False
    if wake:
        notify_wake(runtime, wake)
    return True


def stopped_text(record, request_id, session_id, prompt_id=None):
    if record.get("request_id") != request_id or record.get("session_id") != session_id:
        raise ValueError("Uncorrelated native stop event")
    if prompt_id is not None and record.get("prompt_id") != prompt_id:
        raise ValueError("Uncorrelated native stop prompt")
    if record.get("event") != "Stop" or record.get("background_pending"):
        raise ValueError(
            "Native turn failed or left background work pending: "
            + str(record.get("error") or record.get("event"))
        )
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Native turn ended without a usable response")
    return text


if __name__ == "__main__":
    # Quiet synchronous command hook: no output and no model-facing context.
    payload = json.loads(sys.stdin.read(8 * 1024 * 1024))
    capture(sys.argv[1], payload)
