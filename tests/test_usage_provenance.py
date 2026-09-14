"""Offline completion provenance tests; no native process or model calls."""

import json
from pathlib import Path
import tempfile
import unittest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings
from claude_native_bridge.usage import usage_for_request


COUNTER_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def status_snapshot(model="claude-sonnet-5"):
    return {
        "session_id": "native-provenance",
        "model": {"id": model},
        "prompt_cache": {"requests": 1},
        "context_window": {
            "current_usage": {
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_creation_input_tokens": 33,
                "cache_read_input_tokens": 44,
            }
        },
        "captured_ns": 1,
    }


class EvidenceNative:
    evidence = "genuine"

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.model = model
        self.session_id = "native-provenance"
        self.runtime = Path(tempfile.mkdtemp(dir=home))
        self.last_usage = None
        self.last_response_source = "respond"

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        if self.evidence != "missing":
            observed = (
                "claude-opus-4-8" if self.evidence == "mismatch" else self.model
            )
            snapshot = status_snapshot(observed)
            (self.runtime / "native-usage.json").write_text(json.dumps(snapshot))
            if self.evidence == "genuine":
                self.last_usage = usage_for_request(
                    snapshot, "native-provenance", self.model, 0
                )
        return {
            "sequence": 1,
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def completion_for(home, evidence):
    EvidenceNative.evidence = evidence
    client = NativeBridgeClient(
        hermes_home=home,
        settings=Settings(development_channels_accepted=True),
        native_factory=EvidenceNative,
    )
    try:
        return client.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello"}],
            extra_body={"hermes_session_id": "provenance-test"},
        )
    finally:
        client.close()


class UsageProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def test_genuine_status_line_evidence_is_attached_with_raw_counters(self):
        completion = completion_for(self.folder.name, "genuine")

        self.assertEqual(
            completion.native_bridge_usage_provenance,
            {
                "source": "native_status_line",
                "correlation_status": "correlated",
                "selected_model": "claude-sonnet-5",
                "observed_model": "claude-sonnet-5",
                "raw_counters": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "cache_creation_input_tokens": 33,
                    "cache_read_input_tokens": 44,
                },
            },
        )

    def test_missing_evidence_stays_unknown_instead_of_becoming_zero(self):
        provenance = completion_for(
            self.folder.name, "missing"
        ).native_bridge_usage_provenance

        self.assertEqual(provenance["correlation_status"], "missing")
        self.assertEqual(provenance["selected_model"], "claude-sonnet-5")
        self.assertIsNone(provenance["observed_model"])
        self.assertEqual(
            provenance["raw_counters"], {key: None for key in COUNTER_KEYS}
        )

    def test_model_mismatch_is_reported_without_attributing_foreign_counters(self):
        provenance = completion_for(
            self.folder.name, "mismatch"
        ).native_bridge_usage_provenance

        self.assertEqual(provenance["correlation_status"], "mismatch")
        self.assertEqual(provenance["selected_model"], "claude-sonnet-5")
        self.assertEqual(provenance["observed_model"], "claude-opus-4-8")
        self.assertEqual(
            provenance["raw_counters"], {key: None for key in COUNTER_KEYS}
        )

    def test_ambiguous_evidence_keeps_all_raw_counters_unknown(self):
        provenance = completion_for(
            self.folder.name, "ambiguous"
        ).native_bridge_usage_provenance

        self.assertEqual(provenance["correlation_status"], "ambiguous")
        self.assertEqual(provenance["observed_model"], "claude-sonnet-5")
        self.assertEqual(
            provenance["raw_counters"], {key: None for key in COUNTER_KEYS}
        )


if __name__ == "__main__":
    unittest.main()
