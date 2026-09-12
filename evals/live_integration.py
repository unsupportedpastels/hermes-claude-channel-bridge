"""Bounded native inference with real Hermes tools and isolated memory/skill writes."""

import json
import os
from pathlib import Path
import secrets
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOME = Path((ROOT / ".private" / "integration-home.txt").read_text().strip())
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(HOME)
mem_token = secrets.token_hex(6)
term_token = "TERMINAL_" + secrets.token_hex(6)
learned = "LEARNED_" + secrets.token_hex(6)
(HOME / "memories").mkdir(exist_ok=True)
(HOME / "memories" / "MEMORY.md").write_text(
    "Integration fixture: bridge_memory_token=" + mem_token + "\n"
)
(HOME / "memories" / "USER.md").write_text(
    "These are isolated integration tests; concise responses are preferred.\n"
)
skill = HOME / "skills" / "bridge-validation" / "SKILL.md"
skill.parent.mkdir(parents=True, exist_ok=True)
skill.write_text(
    """---
name: bridge-validation
description: Use when running the native bridge validation test.
---
# Native bridge validation

Execute these steps using Hermes tools, not native Claude tools:
1. Call terminal once with command: python3 -c "print("""
    + "'"
    + term_token
    + "'"
    + """)"
2. Call memory with action add, target memory, and content "Bridge integration learned marker: """
    + learned
    + """".
3. Call skill_manage to patch THIS skill: name bridge-validation, action patch. Replace only the progress sentence at the end with "Verified with real Hermes tools.". Use its existing sentence as old_string.
4. Respond exactly: BRIDGE_OK memory=<bridge_memory_token from Hermes memory> terminal=<terminal output> learned="""
    + learned
    + """
Do not run any other terminal commands or edit files outside these tools.

## Progress
Pending verification.
"""
)

from run_agent import AIAgent


def make_agent(suffix):
    return AIAgent(
        provider="claude-native-bridge",
        model="claude-sonnet-5",
        api_mode="chat_completions",
        session_id="bridge-live-" + suffix + "-" + str(uuid.uuid4()),
        enabled_toolsets=["terminal", "skills", "memory"],
        skip_context_files=True,
        skip_memory=False,
        skip_background_review=True,
        max_iterations=7,
        quiet_mode=True,
        reasoning_config={"effort": "low"},
    )


def tool_names(messages):
    return [
        tc["function"]["name"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls", [])
    ]


def launches():
    return sorted(
        str(p) for p in (HOME / "claude-native-bridge" / "runs").glob("*/launch.json")
    )


report: dict = {
    "home": str(HOME),
    "fixture_memory": mem_token,
    "fixture_terminal": term_token,
    "fixture_learned": learned,
}
a = make_agent("main")
try:
    first = a.run_conversation(
        "Load the bridge-validation skill using skill_view and carry out its instructions exactly. Use the bridge_memory_token already in your Hermes memory; do not read memory files or use any unrelated tools."
    )
    (ROOT / ".private" / "live-first.json").write_text(
        json.dumps(first, indent=2, default=str)
    )
    report["first_final"] = first.get("final_response")
    report["first_tool_names"] = tool_names(first.get("messages", []))
    report["native_launches_after_first"] = launches()
    report["foreground_native_sessions"] = [
        p
        for p in launches()
        if (json.loads(Path(p).read_text()).get("hermes_session_id") or "").startswith(
            "bridge-live-main-"
        )
    ]
    assert len(report["foreground_native_sessions"]) == 1, (
        "Foreground tool rounds rebuilt native sessions"
    )
    assert set(report["first_tool_names"]) == {
        "skill_view",
        "terminal",
        "memory",
        "skill_manage",
    }, report
    assert report["first_tool_names"].count("terminal") == 1, report
    assert all(
        t in first.get("final_response", "") for t in (mem_token, term_token, learned)
    ), report
    assert learned in (HOME / "memories" / "MEMORY.md").read_text(), (
        "Memory was not persisted"
    )
    assert "Verified with real Hermes tools." in skill.read_text(), (
        "Skill patch was not persisted"
    )
    second = a.run_conversation(
        "No tools. Reply exactly FOLLOWUP_OK.", conversation_history=first["messages"]
    )
    report["followup_final"] = second.get("final_response")
    report["native_launches_after_followup"] = launches()
    assert report["followup_final"].strip() == "FOLLOWUP_OK", report
    assert (
        report["native_launches_after_followup"]
        == report["native_launches_after_first"]
    ), "Follow-up rebuilt native session"
finally:
    a.close()

b = make_agent("fresh")
try:
    third = b.run_conversation(
        "No tools. Read your injected Hermes memory and return only the complete value of the Bridge integration learned marker."
    )
    report["fresh_memory_final"] = third.get("final_response")
    assert learned == report["fresh_memory_final"].strip(), report
finally:
    b.close()
report["status"] = "PASS"
(ROOT / ".private" / "live-integration-report.json").write_text(
    json.dumps(report, indent=2)
)
print(json.dumps(report, indent=2))
