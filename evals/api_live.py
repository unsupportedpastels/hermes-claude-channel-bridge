"""Real Sonnet-only API/SSE and unmodified-Hermes integration, isolated stores."""

import json
import os
from pathlib import Path
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="hcb-api-live-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(HOME)
(HOME / "plugins").mkdir()
(HOME / "plugins" / "claude-native-bridge").symlink_to(ROOT, target_is_directory=True)
(HOME / "config.yaml").write_text(
    json.dumps(
        {
            "model": {"provider": "openai-codex", "default": "unchanged-default"},
            "plugins": {"enabled": ["claude-native-bridge"]},
            "claude_native_bridge": {
                "development_channels_accepted": True,
                "effort": "low",
                "request_timeout": 90,
                "retain_diagnostics": True,
            },
            "auxiliary": {"background_review": {"enabled": False}},
            "compression": {"enabled": False},
        }
    )
)
from claude_native_bridge.api_service import setup, stop_server
from claude_native_bridge.api_provider import make_profile
from claude_native_bridge.api_config import TOKEN_ENV

report: dict = {"home": str(HOME), "model": "claude-sonnet-5"}
client = None
try:
    info = setup(HOME)
    profile = make_profile(HOME)
    client = profile.create_client(
        api_key=os.environ[TOKEN_ENV], base_url=info["base_url"], max_retries=0
    )
    models = [m.id for m in client.models.list()]
    report["models"] = models
    marker = "KEEP_" + uuid.uuid4().hex[:10]
    messages = [
        {
            "role": "user",
            "content": "Write six numbered paragraphs explaining Git branches, around 70 words each. Include "
            + marker
            + " in the first paragraph. Use ordinary answer text, not tools.",
        }
    ]
    started = time.monotonic()
    parts = []
    first = None
    usage = None
    finish = None
    stream = client.chat.completions.create(
        model="claude-sonnet-5",
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        reasoning_effort="low",
        extra_body={"hermes_session_id": "api-live-main"},
    )
    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage.model_dump()
        for choice in chunk.choices:
            if choice.delta.content:
                first = time.monotonic() if first is None else first
                parts.append(choice.delta.content)
            if choice.finish_reason:
                finish = choice.finish_reason
    done = time.monotonic()
    text = "".join(parts)
    assert finish == "stop" and len(parts) > 1 and first < done - 0.2, {
        "finish": finish,
        "chunks": len(parts),
    }
    report["stream"] = {
        "chunks": len(parts),
        "first_text_seconds": round(first - started, 3),
        "complete_seconds": round(done - started, 3),
        "early_text_seconds": round(done - first, 3),
        "usage": usage,
    }
    (ROOT / ".private" / "api-live-answer.md").write_text(text)
    before = set((HOME / "claude-native-bridge" / "runs").glob("*/launch.json"))
    follow = client.chat.completions.create(
        model="claude-sonnet-5",
        messages=messages
        + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "Return only the KEEP_ marker from the first paragraph. No tools.",
            },
        ],
        reasoning_effort="low",
        extra_body={"hermes_session_id": "api-live-main"},
    )
    assert follow.choices[0].message.content.strip() == marker
    assert (
        set((HOME / "claude-native-bridge" / "runs").glob("*/launch.json")) == before
    ), "Native session restarted"
    report["same_session_followup"] = True
    from run_agent import AIAgent

    agent = AIAgent(
        provider="claude-native-bridge",
        model="claude-sonnet-5",
        api_mode="chat_completions",
        session_id="api-host-" + uuid.uuid4().hex,
        enabled_toolsets=["terminal"],
        skip_memory=True,
        skip_context_files=True,
        skip_background_review=True,
        max_iterations=3,
        quiet_mode=True,
        reasoning_config={"effort": "low"},
    )
    try:
        result = agent.run_conversation(
            "Run terminal exactly once with command printf API_TOOL_OK. Do not use any other tools. Then reply exactly API_HOST_OK."
        )
        report["host_final"] = result["final_response"]
        report["host_calls"] = [
            t["function"]["name"]
            for m in result["messages"]
            if m.get("role") == "assistant"
            for t in m.get("tool_calls", [])
        ]
        assert report["host_final"].strip() == "API_HOST_OK" and report[
            "host_calls"
        ] == ["terminal"], report
        report["host_usage"] = {
            k: result.get(k)
            for k in [
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ]
        }
    finally:
        agent.close()
    report["status"] = "PASS"
except Exception as exc:
    report["status"] = "FAIL"
    report["error"] = repr(exc)
    raise
finally:
    if client:
        client.close()
    report["cleanup"] = stop_server(HOME)
    (ROOT / ".private" / "api-live-report.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    print(json.dumps(report, indent=2, default=str))
