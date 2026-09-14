"""Pure launch-policy tests for Hermes-owned memory."""

import pytest

from claude_native_bridge.native import native_child_environment
from claude_native_bridge.settings import Settings


@pytest.mark.parametrize(
    ("platform", "source"),
    [
        (
            "linux",
            {
                "HOME": "/home/test",
                "PATH": "/bin",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
            },
        ),
        (
            "darwin",
            {
                "HOME": "/Users/test",
                "PATH": "/usr/bin",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "false",
            },
        ),
        (
            "win32",
            {
                "USERPROFILE": r"C:\Users\test",
                "PATH": r"C:\Windows\System32",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
            },
        ),
    ],
)
@pytest.mark.parametrize("native_auto_compact", [False, True])
def test_all_native_sessions_disable_auto_memory_even_with_hostile_inheritance(
    platform, source, native_auto_compact
):
    env = native_child_environment(
        Settings(native_auto_compact=native_auto_compact),
        source,
        platform=platform,
    )

    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["PATH"] == source["PATH"]
    if platform == "win32":
        assert env["HOME"] == source["USERPROFILE"]
    else:
        assert env["HOME"] == source["HOME"]


def test_native_memory_policy_does_not_mutate_parent_hermes_environment():
    source = {
        "HOME": "/home/test",
        "PATH": "/bin",
        "HERMES_HOME": "/hermes",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
    }
    original = dict(source)

    env = native_child_environment(Settings(), source, platform="linux")

    assert source == original
    assert source["HERMES_HOME"] == "/hermes"
    assert env is not source
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
