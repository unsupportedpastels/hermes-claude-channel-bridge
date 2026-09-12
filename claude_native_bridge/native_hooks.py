"""Record documented native Stop/StopFailure events, without running inference."""

import json
import os
import sys
import time
from pathlib import Path

MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def capture_display(runtime, payload, current):
    # Serialize bounded appends across command-hook processes, including on Windows.
    lock = runtime / "native-text.lock"
    deadline = time.monotonic() + 1
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                (runtime / "native-text-error").touch(mode=0o600)
                return False
            time.sleep(0.005)
    try:
        record = {
            key: payload.get(key)
            for key in (
                "session_id",
                "turn_id",
                "message_id",
                "index",
                "final",
                "delta",
            )
        }
        record["request_id"] = current["request_id"]
        raw = (json.dumps(record) + "\n").encode("utf-8")
        target = runtime / "native-text.jsonl"
        size = target.stat().st_size if target.exists() else 0
        if size + len(raw) > MAX_CAPTURE_BYTES:
            (runtime / "native-text-error").touch(mode=0o600)
            return False
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as output:
            output.write(raw)
        return True
    except Exception:
        (runtime / "native-text-error").touch(mode=0o600)
        raise
    finally:
        lock.rmdir()


def capture(runtime, payload):
    runtime = Path(runtime)
    current = json.loads((runtime / "active-request.json").read_text())
    if payload.get("session_id") != current["session_id"]:
        return False
    event = payload.get("hook_event_name")
    if event == "MessageDisplay":
        return capture_display(runtime, payload, current)
    if event not in ("Stop", "StopFailure"):
        return False
    record = {
        "request_id": current["request_id"],
        "session_id": current["session_id"],
        "event": event,
        "text": payload.get("last_assistant_message"),
        "error": payload.get("error"),
        "background_pending": bool(
            payload.get("background_tasks") or payload.get("session_crons")
        ),
    }
    target = runtime / "native-stop.json"
    temporary = runtime / "native-stop.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.replace(temporary, target)
    return True


def stopped_text(record, request_id, session_id):
    if record.get("request_id") != request_id or record.get("session_id") != session_id:
        raise ValueError("Uncorrelated native stop event")
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
    # Quiet command hook: no output, no decision forcing another model request.
    payload = json.loads(sys.stdin.read(8 * 1024 * 1024))
    capture(sys.argv[1], payload)
