"""Private rolling bridge events contain metadata, never request content."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from claude_native_bridge.event_log import (
    close_event_log, configure_event_log, event_trace, record_event, safe_run,
)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(close_event_log)

    def test_rollover_is_bounded_and_accepts_only_safe_fields(self):
        root = Path(self.folder.name) / "private-api"
        configure_event_log(root, max_bytes=512, backup_count=2)
        with self.assertRaises(ValueError):
            record_event("generation_failed", detail="sensitive prompt or credential")
        for index in range(100):
            record_event(
                "generation_failed",
                trace=f"{index:012x}",
                reason="unknown_tool",
                error_type="ProtocolError",
                run="session-test123",
                stream=True,
            )
        files = sorted(root.glob("events.jsonl*"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(file.stat().st_size <= 512 for file in files))
        text = "".join(file.read_text() for file in files)
        self.assertNotIn("sensitive prompt or credential", text)
        self.assertIn("000000000063", text)
        for line in text.splitlines():
            row = json.loads(line)
            self.assertEqual(row["event"], "generation_failed")
            self.assertTrue(row["time"].endswith("Z"))
            self.assertEqual(row["reason"], "unknown_tool")

    def test_untrusted_values_and_unknown_events_cannot_be_recorded(self):
        configure_event_log(Path(self.folder.name) / "private-api")
        with self.assertRaises(ValueError):
            record_event("user message", trace="safe")
        with self.assertRaises(ValueError):
            record_event("generation_failed", run="session-../../token")
        with self.assertRaises(ValueError):
            record_event("generation_failed", error_type="token=secret")
        with self.assertRaises(ValueError):
            record_event("generation_failed", reason="a secret from an exception")

    def test_trace_reaches_worker_thread_and_invalid_runtime_name_is_omitted(self):
        root = Path(self.folder.name) / "private-api"
        log = configure_event_log(root)
        with event_trace("123456abcdef"):
            asyncio.run(asyncio.to_thread(
                record_event, "native_failure", branch="uncertain", reason="other",
                error_type="ProtocolError", run=safe_run("session-valid_1"),
            ))
        record_event("service_started", capacity=10)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows[0]["trace"], "123456abcdef")
        self.assertEqual(rows[0]["run"], "session-valid_1")
        self.assertNotIn("trace", rows[1])
        self.assertIsNone(safe_run("session-../../credential"))

    def test_native_failure_records_classified_reason_and_run_without_message(self):
        from claude_native_bridge.client import _record_native_failure

        log = configure_event_log(Path(self.folder.name) / "private-api")
        native = type("Native", (), {"runtime": Path(self.folder.name) / "session-abc123"})()
        with event_trace("123456abcdef"):
            _record_native_failure(
                native,
                ValueError("Missing or out-of-order native text batch; secret payload"),
                "uncertain",
            )
        row = json.loads(log.read_text().strip())
        self.assertEqual(row["trace"], "123456abcdef")
        self.assertEqual(row["run"], "session-abc123")
        self.assertEqual(row["reason"], "text_batch_order")
        self.assertEqual(row["error_type"], "ValueError")
        self.assertNotIn("secret payload", log.read_text())


if __name__ == "__main__":
    unittest.main()
