"""Read native Claude's documented status-line counters; never infer missing usage."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
PROVENANCE_SOURCE = "native_status_line"


def _unknown_counters():
    return {field: None for field in TOKEN_FIELDS}


def _status_snapshot(runtime):
    if runtime is None:
        return None
    try:
        snapshot = json.loads((Path(runtime) / "native-usage.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def completion_usage_provenance(selected_model, usage, runtime=None, session_id=None):
    """Describe request attribution without claiming billing or exposing payload data."""
    observed = None
    snapshot = _status_snapshot(runtime)
    if snapshot is not None:
        model = snapshot.get("model")
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            observed = model["id"]

    counters = _unknown_counters()
    correlation = "missing" if snapshot is None else "ambiguous"
    if snapshot is not None and (
        (observed is not None and observed != selected_model)
        or (
            session_id is not None
            and snapshot.get("session_id") != session_id
        )
    ):
        correlation = "mismatch"
    elif usage is not None and snapshot is not None:
        context_window = snapshot.get("context_window")
        raw = (
            context_window.get("current_usage")
            if isinstance(context_window, dict)
            else None
        )
        if isinstance(raw, dict) and all(
            type(raw.get(field)) is int and raw[field] >= 0
            for field in TOKEN_FIELDS
        ):
            counters = {field: raw[field] for field in TOKEN_FIELDS}
            correlation = "correlated"

    return {
        "source": PROVENANCE_SOURCE,
        "correlation_status": correlation,
        "selected_model": selected_model,
        "observed_model": observed,
        "raw_counters": counters,
    }


def usage_for_request(snapshot, session_id, model, previous_requests):
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
        return None
    observed_model = snapshot.get("model")
    if not isinstance(observed_model, dict) or observed_model.get("id") != model:
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


def context_occupancy(snapshot, session_id, model):
    """Native context consumed after the last API call, or None without evidence.

    Tokens come from Claude's own status-line counters, never a local estimate.
    ``window`` is the reported context_window_size or None when absent.
    """
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
        return None
    observed_model = snapshot.get("model")
    if not isinstance(observed_model, dict) or observed_model.get("id") != model:
        return None
    context_window = snapshot.get("context_window")
    if not isinstance(context_window, dict):
        return None
    usage = context_window.get("current_usage")
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0 for k in TOKEN_FIELDS
    ):
        return None
    size = context_window.get("context_window_size")
    return {
        "tokens": sum(usage[field] for field in TOKEN_FIELDS),
        "window": size if type(size) is int and size > 0 else None,
    }


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
                ),
                "context_window_size": (payload.get("context_window") or {}).get(
                    "context_window_size"
                ),
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
