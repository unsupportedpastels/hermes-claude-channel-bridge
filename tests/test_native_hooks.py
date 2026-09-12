import json
from pathlib import Path
import tempfile
import unittest
from claude_native_bridge.native_hooks import capture, stopped_text


class NativeHookTests(unittest.TestCase):
    def test_real_final_text_is_preserved_but_api_error_is_never_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "active-request.json").write_text(
                json.dumps({"request_id": "r", "session_id": "s"})
            )
            self.assertTrue(
                capture(
                    root,
                    {
                        "session_id": "s",
                        "hook_event_name": "Stop",
                        "last_assistant_message": "A native refusal.",
                    },
                )
            )
            record = json.loads((root / "native-stop.json").read_text())
            self.assertEqual(stopped_text(record, "r", "s"), "A native refusal.")
            self.assertEqual((root / "native-stop.json").stat().st_mode & 0o777, 0o600)
            capture(
                root,
                {
                    "session_id": "s",
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
            (root / "active-request.json").write_text(
                json.dumps({"request_id": "r", "session_id": "s"})
            )
            self.assertFalse(
                capture(
                    root,
                    {
                        "session_id": "foreign",
                        "hook_event_name": "Stop",
                        "last_assistant_message": "wrong",
                    },
                )
            )
            self.assertFalse((root / "native-stop.json").exists())
            record = {
                "request_id": "r",
                "session_id": "s",
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
