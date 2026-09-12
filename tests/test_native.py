import unittest
from pathlib import Path
from unittest.mock import patch
from claude_native_bridge import models, native
from claude_native_bridge.settings import Settings, NativeBridgeError

from claude_native_bridge.native import native_argv, child_environment, consent_key


class NativeLaunchTests(unittest.TestCase):
    def test_native_uses_shared_model_catalog(self):
        self.assertIs(native.MODELS, models.MODELS)

    def test_missing_dependencies_fail_before_native_launch(self):
        session = native.NativeSession(
            Settings(development_channels_accepted=True),
            Path("/tmp/unused"),
            "claude-sonnet-5",
            "medium",
            http_client=object(),
        )
        with patch.object(native.shutil, "which", return_value=None):
            with self.assertRaisesRegex(NativeBridgeError, "Claude Code"):
                session.start()

    def test_consent_follows_observed_selection_not_assumed_key_delivery(self):
        text = "Accessing workspace:\n/tmp/owned\n❯ No, exit\nYes, I trust this folder"
        self.assertEqual(consent_key(text, "/tmp/owned"), ("trust", "Down"))
        self.assertEqual(consent_key(text, "/tmp/owned"), ("trust", "Down"))
        text = "Accessing workspace:\n/tmp/owned\nNo, exit\n❯ Yes, I trust this folder"
        self.assertEqual(consent_key(text, "/tmp/owned"), ("trust", "Enter"))
        self.assertIsNone(consent_key(text, "/tmp/other"))

    def test_haiku_does_not_receive_unsupported_effort(self):
        args = native_argv(
            "/bin/claude",
            "test-id",
            "/tmp/mcp.json",
            "claude-haiku-4-5-20251001",
            "medium",
        )
        self.assertNotIn("--effort", args)

    def test_native_only_and_exact_model_effort(self):
        args = native_argv(
            "/bin/claude", "test-id", "/tmp/test/mcp.json", "claude-fable-5-1", "medium"
        )
        self.assertNotIn("-p", args)
        self.assertNotIn("--print", args)
        self.assertEqual(args[args.index("--model") + 1], "claude-fable-5-1")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(
            args[args.index("--allowedTools") + 1], "mcp__hermesbridge__respond"
        )
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_child_environment_does_not_inherit_api_or_host_secrets(self):
        env = child_environment(
            {
                "HOME": "/home/test",
                "PATH": "/bin",
                "ANTHROPIC_API_KEY": "fake",
                "ANTHROPIC_BASE_URL": "https://wrong.invalid",
                "OTHER_SECRET": "fake",
                "LANG": "C.UTF-8",
            }
        )
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("OTHER_SECRET", env)
        self.assertEqual(env["HOME"], "/home/test")
        self.assertEqual(env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"], "0")


if __name__ == "__main__":
    unittest.main()
