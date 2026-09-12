"""Run Hermes' actual review worker, proving fork writes and foreground isolation."""

import copy
import json
import os
from pathlib import Path
import secrets
import threading
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOME = Path((ROOT / ".private" / "integration-home.txt").read_text().strip())
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(HOME)
fg = "FG_" + secrets.token_hex(6)
bg = "BG_" + secrets.token_hex(6)
skill_name = "bridge-bg-" + secrets.token_hex(4)
skill = HOME / "skills" / skill_name / "SKILL.md"
from model_tools import handle_function_call

created = json.loads(
    handle_function_call(
        "skill_manage",
        {
            "operations": [
                {
                    "action": "create",
                    "name": skill_name,
                    "content": "---\nname: "
                    + skill_name
                    + "\ndescription: Use when testing isolated background learning.\n---\n# Background fixture\n\nPending background verification.\n",
                }
            ]
        },
    )
)
assert created.get("success"), created
# Explicitly opt this disposable fixture into curator management through the
# supported CLI; foreground-created skills are deliberately user-owned by default.
adopted = subprocess.run(
    ["hermes", "curator", "adopt", skill_name],
    capture_output=True,
    text=True,
    check=True,
)
from run_agent import AIAgent
from agent.background_review import _run_review_in_thread

a = AIAgent(
    provider="claude-native-bridge",
    model="claude-sonnet-5",
    api_mode="chat_completions",
    session_id="bridge-background-" + str(uuid.uuid4()),
    enabled_toolsets=["memory", "skills"],
    skip_context_files=True,
    skip_background_review=True,
    max_iterations=5,
    quiet_mode=True,
    reasoning_config={"effort": "low"},
)
report = {
    "foreground_marker": fg,
    "background_marker": bg,
    "hermes_session_id": a.session_id,
}
try:
    first = a.run_conversation(
        "Remember this only in this conversation: "
        + fg
        + ". Do not use tools. Reply READY."
    )
    snapshot = copy.deepcopy(first["messages"])
    before = set((HOME / "claude-native-bridge" / "runs").glob("*/launch.json"))
    prompt = (
        "This is an authorized integration test in temporary stores. Use memory action add target memory "
        "to save exactly Background review fixture: "
        + bg
        + ". Load "
        + skill_name
        + " using skill_view, "
        "then use skill_manage to patch its only progress sentence to Background review verified. "
        "Do not modify any other skill or memory. Finish REVIEW_DONE."
    )
    worker = threading.Thread(
        target=_run_review_in_thread,
        args=(a, snapshot, prompt),
        kwargs={"review_memory": True},
        daemon=True,
    )
    worker.start()
    worker.join(150)
    if worker.is_alive():
        a.interrupt()
        worker.join(8)
        raise TimeoutError("Background review exceeded test budget")
    assert bg in (HOME / "memories" / "MEMORY.md").read_text(), (
        "Review did not save the memory"
    )
    assert "Background review verified." in skill.read_text(), (
        "Review did not update the skill"
    )
    assert first["messages"] == snapshot, "Review mutated parent messages"
    after = set((HOME / "claude-native-bridge" / "runs").glob("*/launch.json"))
    review_paths = after - before
    assert len(review_paths) == 1, "Expected one isolated native review session"
    report["review_native_launch"] = str(next(iter(review_paths)))
    report["review_shares_hermes_id"] = (
        json.loads(next(iter(review_paths)).read_text())["hermes_session_id"]
        == a.session_id
    )
    assert report["review_shares_hermes_id"]
    assert not any((p.parent / "ready.json").exists() for p in review_paths), (
        "Review left a native session running"
    )
    second = a.run_conversation(
        "No tools. Return the FG_ marker from our earlier conversation, and nothing else.",
        conversation_history=first["messages"],
    )
    assert second["final_response"].strip() == fg, second.get("final_response")
    assert (
        set((HOME / "claude-native-bridge" / "runs").glob("*/launch.json")) == after
    ), "Foreground native context was lost"
    report["foreground_after_review"] = second["final_response"]
    report["memory_write_verified"] = True
    report["skill_write_verified"] = True
    report["status"] = "PASS"
finally:
    a.close()
(ROOT / ".private" / "background-review-report.json").write_text(
    json.dumps(report, indent=2)
)
print(json.dumps(report, indent=2))
