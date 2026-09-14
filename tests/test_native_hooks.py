import json
import sys
import tempfile
import unittest
from pathlib import Path

from claude_native_bridge.native_hooks import capture, open_request, stopped_text
from claude_native_bridge.windows_security import (
    assert_private_file,
    secure_runtime_directory,
)


def submit(root, session="s", prompt_id="prompt-r"):
    return capture(
        root,
        {
            "session_id": session,
            "prompt_id": prompt_id,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "request",
        },
    )


class NativeHookTests(unittest.TestCase):
    def test_real_final_text_is_preserved_but_api_error_is_never_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if sys.platform == "win32":
                secure_runtime_directory(root)
            open_request(root, "s", "r")
            self.assertTrue(submit(root))
            self.assertTrue(
                capture(
                    root,
                    {
                        "session_id": "s",
                        "prompt_id": "prompt-r",
                        "hook_event_name": "MessageDisplay",
                        "turn_id": "turn-r",
                        "message_id": "message-r",
                        "index": 0,
                        "final": True,
                        "delta": "A native refusal.",
                    },
                )
            )
            self.assertTrue(
                capture(
                    root,
                    {
                        "session_id": "s",
                        "prompt_id": "prompt-r",
                        "hook_event_name": "Stop",
                        "last_assistant_message": "A native refusal.",
                    },
                )
            )
            record = json.loads((root / "native-stop.json").read_text())
            self.assertEqual(stopped_text(record, "r", "s"), "A native refusal.")
            self.assertEqual(record["turn_id"], "turn-r")
            if sys.platform == "win32":
                assert_private_file(root / "native-stop.json")
            else:
                self.assertEqual((root / "native-stop.json").stat().st_mode & 0o777, 0o600)
            capture(
                root,
                {
                    "session_id": "s",
                    "prompt_id": "prompt-r",
                    "hook_event_name": "StopFailure",
                    "error": "rate_limit",
                    "last_assistant_message": "API Error",
                },
            )
            with self.assertRaisesRegex(ValueError, "rate_limit"):
                stopped_text(
                    json.loads((root / "native-stop.json").read_text()), "r", "s"
                )

    def test_foreign_session_stale_request_or_pending_background_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if sys.platform == "win32":
                secure_runtime_directory(root)
            open_request(root, "s", "r")
            self.assertTrue(submit(root))
            self.assertFalse(
                capture(
                    root,
                    {
                        "session_id": "foreign",
                        "prompt_id": "prompt-r",
                        "hook_event_name": "Stop",
                        "last_assistant_message": "wrong",
                    },
                )
            )
            self.assertFalse((root / "native-stop.json").exists())
            record = {
                "request_id": "r",
                "session_id": "s",
                "prompt_id": "prompt-r",
                "event": "Stop",
                "text": "done",
                "background_pending": True,
            }
            with self.assertRaises(ValueError):
                stopped_text(record, "r", "s")
            record["background_pending"] = False
            with self.assertRaises(ValueError):
                stopped_text(record, "stale", "s")


if __name__ == "__main__":
    unittest.main()
