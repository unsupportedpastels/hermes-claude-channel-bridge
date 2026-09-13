import json
from pathlib import Path
import tempfile
import sys
import unittest

from claude_native_bridge.usage import capture_status, usage_for_request
from claude_native_bridge.windows_security import assert_private_file, secure_runtime_directory


def snapshot(requests=1):
    return {
        "session_id": "native-test",
        "model": {"id": "claude-sonnet-5"},
        "prompt_cache": {"requests": requests},
        "context_window": {
            "current_usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 900,
            }
        },
    }


class UsageTests(unittest.TestCase):
    def test_maps_real_input_buckets_to_openai_usage_without_double_counting(self):
        data = snapshot()
        result = usage_for_request(data, "native-test", "claude-sonnet-5", 0)
        self.assertEqual(result.prompt_tokens, 1010)
        self.assertEqual(result.completion_tokens, 20)
        self.assertEqual(result.total_tokens, 1030)
        self.assertEqual(result.prompt_tokens_details.cached_tokens, 900)
        self.assertEqual(result.prompt_tokens_details.cache_write_tokens, 100)

    def test_stale_skipped_or_foreign_counters_remain_unknown(self):
        for requests, session, model in [
            (0, "native-test", "claude-sonnet-5"),
            (2, "native-test", "claude-sonnet-5"),
            (1, "foreign", "claude-sonnet-5"),
            (1, "native-test", "other"),
        ]:
            with self.subTest(requests=requests, session=session, model=model):
                self.assertIsNone(
                    usage_for_request(snapshot(requests), session, model, 0)
                )

    def test_missing_or_invalid_counts_are_not_fabricated_as_zero(self):
        for value in [None, -1, True, "900"]:
            data = snapshot()
            data["context_window"]["current_usage"]["cache_read_input_tokens"] = value
            self.assertIsNone(
                usage_for_request(data, "native-test", "claude-sonnet-5", 0)
            )
        data = snapshot()
        data["context_window"]["current_usage"] = None
        self.assertIsNone(usage_for_request(data, "native-test", "claude-sonnet-5", 0))
        data = snapshot()
        data["context_window"]["current_usage"]["cache_read_input_tokens"] = 0
        self.assertEqual(
            usage_for_request(
                data, "native-test", "claude-sonnet-5", 0
            ).prompt_tokens_details.cached_tokens,
            0,
        )

    def test_capture_keeps_only_usage_metadata_in_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if sys.platform == "win32":
                secure_runtime_directory(root)
            (root / "launch.json").write_text(json.dumps({"session_id": "native-test"}))
            data = snapshot()
            data["workspace"] = {"sensitive": "not needed"}
            data["cost"] = {"unrelated": 123}
            self.assertTrue(capture_status(root, data))
            saved = json.loads((root / "native-usage.json").read_text())
            self.assertNotIn("workspace", saved)
            self.assertNotIn("cost", saved)
            if sys.platform == "win32":
                assert_private_file(root / "native-usage.json")
            else:
                self.assertEqual((root / "native-usage.json").stat().st_mode & 0o777, 0o600)
            data["session_id"] = "foreign"
            self.assertFalse(capture_status(root, data))


if __name__ == "__main__":
    unittest.main()
