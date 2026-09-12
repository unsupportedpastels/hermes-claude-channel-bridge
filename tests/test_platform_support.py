import base64
import json
from pathlib import Path
import tempfile
import unittest

from claude_native_bridge.platform_support import native_environment, script_command
from claude_native_bridge.windows_controller import WindowsController


class PlatformPolicyTests(unittest.TestCase):
    def test_windows_environment_keeps_os_prerequisites_but_not_credentials(self):
        source = {
            "SystemRoot": "C:\\Windows",
            "USERPROFILE": "C:\\Users\\Test",
            "Path": "C:\\Bin",
            "COMSPEC": "C:\\Windows\\System32\\cmd.exe",
            "ANTHROPIC_API_KEY": "not-forwarded",
            "CLAUDE_NATIVE_BRIDGE_API_KEY": "not-forwarded",
            "OTHER_SECRET": "not-forwarded",
        }
        result = native_environment(source, platform="win32")
        self.assertEqual(result["SystemRoot"], source["SystemRoot"])
        self.assertEqual(result["HOME"], source["USERPROFILE"])
        self.assertEqual(result["PYTHONUTF8"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertNotIn("CLAUDE_NATIVE_BRIDGE_API_KEY", result)
        self.assertNotIn("OTHER_SECRET", result)

    def test_windows_hook_quoting_is_native_powershell_without_policy_override(self):
        command = script_command(
            "C:/Program Files/Python/python.exe",
            "C:/O'Name/hook.py",
            "C:/runtime",
            platform="win32",
        )
        self.assertTrue(
            command.startswith(
                "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
            )
        )
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("'C:/O''Name/hook.py'", script)
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertNotIn("ExecutionPolicy", command)

    def test_control_adapter_uses_the_native_terminal_interface(self):
        calls = []

        class Fake:
            def __init__(self, **kwargs):
                self.alive = False
                calls.append(("init", kwargs))

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def capture(self):
                return "consent screen"

            def send_key(self, key):
                calls.append(("key", key))

            def close(self):
                self.alive = False

        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p / "launch.json").write_text(
                json.dumps(
                    {
                        "argv": ["claude.exe"],
                        "environment": {"HOME": "x"},
                        "owner_pid": 123,
                    }
                )
            )
            controller = WindowsController(p, factory=Fake)
            self.assertEqual(
                controller.command("has-session", check=False).returncode, 1
            )
            controller.command("new-session")
            self.assertEqual(
                controller.command("capture-pane").stdout, "consent screen"
            )
            controller.command("send-keys", "-t", "worker", "Enter")
            self.assertIn(("key", "Enter"), calls)
            controller.command("kill-server")
            self.assertEqual(
                controller.command("has-session", check=False).returncode, 1
            )


if __name__ == "__main__":
    unittest.main()
