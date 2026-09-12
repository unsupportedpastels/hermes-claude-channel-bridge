"""Verify memory/skills and the actual review worker through the local API."""

import json
import os
from pathlib import Path
import threading
import subprocess
import atexit
import uuid
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(json.loads((ROOT / ".private/api-live-report.json").read_text())["home"])
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(HOME)
os.environ.update(
    {k: v for k, v in dotenv_values(HOME / ".env").items() if v is not None}
)
from claude_native_bridge.api_service import ensure_server, stop_server
from claude_native_bridge.api_config import TOKEN_ENV

ensure_server(HOME, os.environ[TOKEN_ENV])
atexit.register(lambda: stop_server(HOME))
from model_tools import handle_function_call
from run_agent import AIAgent
from agent.background_review import _run_review_in_thread

base = "BASE_" + uuid.uuid4().hex[:8]
learn = "LEARN_" + uuid.uuid4().hex[:8]
review = "REVIEW_" + uuid.uuid4().hex[:8]
(HOME / "memories").mkdir(exist_ok=True)
(HOME / "memories/MEMORY.md").write_text("Integration baseline marker: " + base)
name = "api-learning-" + uuid.uuid4().hex[:6]
content = (
    "---\nname: "
    + name
    + "\ndescription: Use for the API learning integration check.\n---\n# Learning\nPending verification.\n"
)
created = json.loads(
    handle_function_call(
        "skill_manage",
        {"operations": [{"name": name, "action": "create", "content": content}]},
    )
)
assert created.get("success"), created
subprocess.run(["hermes", "curator", "adopt", name], check=True, capture_output=True)
report: dict = {"model": "claude-sonnet-5", "home": str(HOME)}
a = AIAgent(
    provider="claude-native-bridge",
    model="claude-sonnet-5",
    api_mode="chat_completions",
    session_id="api-learning-" + uuid.uuid4().hex,
    enabled_toolsets=["memory", "skills"],
    skip_context_files=True,
    skip_background_review=True,
    max_iterations=6,
    quiet_mode=True,
    reasoning_config={"effort": "low"},
)
try:
    result = a.run_conversation(
        "Load "
        + name
        + " with skill_view. Save the memory note "
        + learn
        + " using memory. Patch that skill's progress sentence to Verified through the API. using skill_manage. Do not use other tools. Then return only the integration baseline marker from your injected memory."
    )
    assert result["final_response"].strip() == base, result["final_response"]
    assert learn in (HOME / "memories/MEMORY.md").read_text()
    assert (
        "Verified through the API." in (HOME / "skills" / name / "SKILL.md").read_text()
    )
    worker = threading.Thread(
        target=_run_review_in_thread,
        args=(
            a,
            result["messages"],
            "Integration review: add exactly the note "
            + review
            + " using memory. Load "
            + name
            + " with skill_view, then patch its progress sentence to Reviewed through the API. using skill_manage. Do not change anything else.",
        ),
        kwargs={"review_memory": True},
        daemon=True,
    )
    worker.start()
    worker.join(75)
    if worker.is_alive():
        a.interrupt()
        worker.join(5)
        raise TimeoutError("Review test exceeded budget")
    assert review in (HOME / "memories/MEMORY.md").read_text()
    assert (
        "Reviewed through the API." in (HOME / "skills" / name / "SKILL.md").read_text()
    )
    report.update(
        memory_read=True,
        memory_write=True,
        skill_read=True,
        skill_write=True,
        background_review_write=True,
        background_skill_update=True,
    )
finally:
    a.close()
b = AIAgent(
    provider="claude-native-bridge",
    model="claude-sonnet-5",
    api_mode="chat_completions",
    session_id="api-recall-" + uuid.uuid4().hex,
    enabled_toolsets=[],
    skip_context_files=True,
    skip_background_review=True,
    max_iterations=2,
    quiet_mode=True,
    reasoning_config={"effort": "low"},
)
try:
    final = b.run_conversation("No tools. Return only the saved REVIEW_ memory note.")[
        "final_response"
    ]
    assert final.strip() == review, final
    report.update(fresh_session_recall=True, status="PASS")
finally:
    b.close()
    report["cleanup"] = stop_server(HOME)
    (ROOT / ".private/api-learning-report.json").write_text(
        json.dumps(report, indent=2)
    )
print(json.dumps(report, indent=2))
