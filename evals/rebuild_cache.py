"""Real-login receipt: prompt-cache reuse when a native session is rebuilt.

A rebuilt session replays canonical history into a fresh native CLI. The CLI's
system prompt names its working directory and a scratchpad path containing its
--session-id, so each rebuild can change the prompt prefix. Four fresh sessions
receive identical history, back to back inside the 5-minute cache window:

  fresh-1, fresh-2   new session id and new run folder (current behavior)
  pinned-1, pinned-2 the same session id and run folder for both

fresh-2 measures today's rebuild reuse; pinned-2 measures reuse once both
per-session values are held fixed. The transcript of pinned-1 is removed before
pinned-2 so the CLI accepts the reused id. Uses the real claude.ai login and
four short Haiku turns. Run on the bridge host:  python evals/rebuild_cache.py
"""

import json
import shutil
import sys
import tempfile
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_native_bridge import native  # noqa: E402
from claude_native_bridge import client as client_module  # noqa: E402
from claude_native_bridge.client import NativeBridgeClient  # noqa: E402
from claude_native_bridge.settings import Settings  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
PINNED_ID = str(uuid.uuid4())
pinned = {"on": False, "dir": None, "requests": False, "count": 0}


def _client_uuid4():
    # The client's k-th id (request ids first) is the same in every pinned run.
    if not pinned["requests"]:
        return uuid.uuid4()
    pinned["count"] += 1
    return uuid.uuid5(uuid.NAMESPACE_URL, "cnb-cache/" + str(pinned["count"]))


client_module.uuid = types.SimpleNamespace(uuid4=_client_uuid4, UUID=uuid.UUID)

# Only this eval's NativeSession sees the pinned id and folder.
native.uuid = types.SimpleNamespace(
    uuid4=lambda: uuid.UUID(PINNED_ID) if pinned["on"] else uuid.uuid4(),
    UUID=uuid.UUID,
)
_mkdtemp = tempfile.mkdtemp


def _session_dir(*args, **kwargs):
    if pinned["on"] and kwargs.get("prefix") == "session-":
        target = Path(kwargs["dir"]) / "session-pinned"
        target.mkdir(mode=0o700)
        pinned["dir"] = target
        return str(target)
    return _mkdtemp(*args, **kwargs)


native.tempfile = types.SimpleNamespace(mkdtemp=_session_dir)


def history():
    topics = ["rivers", "bridges", "orchards", "lighthouses", "glaciers", "libraries"]
    messages = [{"role": "system", "content": "You are a concise assistant for a public cache test."}]
    for i in range(24):
        topic = topics[i % len(topics)]
        body = " ".join(f"Public note {i}.{j} about {topic} and their upkeep." for j in range(60))
        messages.append({"role": "user", "content": f"Question {i}: summarise this. {body}"})
        messages.append({"role": "assistant", "content": f"Summary {i}: upkeep of {topic} matters."})
    messages.append({"role": "user", "content": "Reply with the single word ok."})
    return messages


def transcript_usage(session_id):
    """Main-turn usage rows the CLI recorded, and the transcript files found."""
    files = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    rows = []
    for file in files:
        for line in file.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            usage = (entry.get("message") or {}).get("usage")
            if entry.get("type") == "assistant" and usage:
                rows.append({k: usage.get(k) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
    return rows, files


def run(label, root, pin, pin_requests):
    pinned["on"] = pin
    pinned["requests"] = pin_requests
    pinned["count"] = 0
    home = root / ("pinned" if pin else label)
    client = NativeBridgeClient(
        settings=Settings(development_channels_accepted=True, request_timeout=120,
                          retain_diagnostics=True),
        hermes_home=home,
    )
    try:
        result = client.chat.completions.create(
            model=MODEL, messages=history(), extra_body={"hermes_session_id": "cache-" + label}
        )
    finally:
        client.close()
    launch = next(home.glob("claude-native-bridge/runs/*/launch.json"))
    session_id = json.loads(launch.read_text())["session_id"]
    rows, files = transcript_usage(session_id)
    receipt = {
        "label": label,
        "session_id_pinned": session_id == PINNED_ID,
        "request_ids_pinned": pin_requests,
        "answer": (result.choices[0].message.content or "")[:40],
        "bridge_usage": json.loads(json.dumps(result.usage, default=vars)) if result.usage else None,
        "transcript_usage": rows,
    }
    # Only this eval's own transcripts are removed; pinned-2 needs the id free.
    for file in files:
        file.unlink()
    if pin and pinned["dir"] is not None:
        for run_dir in pinned["dir"].parent.glob("session-pinned*"):
            shutil.rmtree(run_dir, ignore_errors=True)
    return receipt


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cnb-cache-") as folder:
        root = Path(folder)
        receipts = []
        wanted = sys.argv[1:] or ["fresh", "pinned"]
        conditions = {
            "fresh": (False, False),
            "pinned": (True, False),
            "request": (False, True),
            "all": (True, True),
        }
        for name in wanted:
            pin, pin_requests = conditions[name]
            # Pinned runs share one Hermes home so their run folder path is identical.
            for index in (1, 2):
                receipts.append(run(f"{name}-{index}", root, pin, pin_requests))
                print(json.dumps(receipts[-1]), flush=True)
