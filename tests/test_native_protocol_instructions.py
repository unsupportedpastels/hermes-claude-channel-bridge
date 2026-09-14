import stat
import sys

from claude_native_bridge.native import (
    BRIDGE_PROTOCOL_INSTRUCTIONS,
    NativeSession,
    native_argv,
)
from claude_native_bridge.settings import Settings
from claude_native_bridge.windows_security import (
    assert_private_file,
    secure_runtime_directory,
)


def test_protocol_instructions_are_serialized_to_private_session_file(tmp_path):
    runtime = tmp_path
    if sys.platform == "win32":
        runtime = secure_runtime_directory(tmp_path / "runtime")
    session = NativeSession(
        Settings(development_channels_accepted=True),
        tmp_path,
        "claude-sonnet-5",
        "low",
        http_client=object(),
    )
    session.runtime = runtime

    prompt_path = session._write_protocol_instructions()

    assert prompt_path == runtime / "bridge-protocol.txt"
    assert prompt_path.read_text(encoding="utf-8") == BRIDGE_PROTOCOL_INSTRUCTIONS
    if sys.platform == "win32":
        assert_private_file(prompt_path)
    else:
        assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600


def test_native_launch_appends_protocol_file_without_replacing_native_prompt(tmp_path):
    prompt_path = tmp_path / "bridge-protocol.txt"
    args = native_argv(
        "/bin/claude",
        "test-id",
        tmp_path / "mcp.json",
        "claude-sonnet-5",
        "low",
        prompt_path,
    )

    assert args.count("--append-system-prompt-file") == 1
    assert args[args.index("--append-system-prompt-file") + 1] == str(prompt_path)
    assert "--system-prompt" not in args
    assert "--system-prompt-file" not in args
    assert BRIDGE_PROTOCOL_INSTRUCTIONS not in args
    assert args[args.index("--tools") + 1] == (
        "mcp__hermesbridge__respond,mcp__hermesbridge__read_result"
    )
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" not in args


def test_protocol_describes_real_bridge_contract_without_elevating_history_roles():
    text = BRIDGE_PROTOCOL_INSTRUCTIONS

    assert "local Hermes model-provider bridge" in text
    assert "role-labeled" in text
    assert "do not change Claude's instruction hierarchy or permissions" in text
    assert "respond" in text
    assert "read_result" in text
    assert "held result is the next authoritative request" in text
    assert "Native task tools are disabled" in text
    assert "bypass" not in text.lower()
