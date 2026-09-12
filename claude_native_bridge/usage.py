"""Read native Claude's documented status-line counters; never infer missing usage."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_for_request(snapshot, session_id, model, previous_requests):
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
        return None
    if (snapshot.get("model") or {}).get("id") != model:
        return None
    count = (snapshot.get("prompt_cache") or {}).get("requests")
    if (
        type(previous_requests) is not int
        or type(count) is not int
        or count != previous_requests + 1
    ):
        return None
    usage = (snapshot.get("context_window") or {}).get("current_usage")
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0 for k in TOKEN_FIELDS
    ):
        return None
    prompt = (
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"]
    )
    return NS(
        prompt_tokens=prompt,
        completion_tokens=usage["output_tokens"],
        total_tokens=prompt + usage["output_tokens"],
        prompt_tokens_details=NS(
            cached_tokens=usage["cache_read_input_tokens"],
            cache_write_tokens=usage["cache_creation_input_tokens"],
        ),
    )


def capture_status(runtime, payload):
    root = Path(runtime)
    try:
        launch = json.loads((root / "launch.json").read_text())
        if not isinstance(payload, dict) or payload.get("session_id") != launch.get(
            "session_id"
        ):
            return False
        data = {
            "session_id": payload["session_id"],
            "model": {"id": (payload.get("model") or {}).get("id")},
            "context_window": {
                "current_usage": (payload.get("context_window") or {}).get(
                    "current_usage"
                )
            },
            "prompt_cache": {
                "requests": (payload.get("prompt_cache") or {}).get("requests")
            },
            "captured_ns": time.monotonic_ns(),
        }
        fd, name = tempfile.mkstemp(prefix=".usage-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
            os.replace(name, root / "native-usage.json")
        finally:
            Path(name).unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


if __name__ == "__main__":
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) <= 1024 * 1024:
            capture_status(sys.argv[1], json.loads(raw))
    except (ValueError, IndexError):
        pass
    print("Hermes bridge")
