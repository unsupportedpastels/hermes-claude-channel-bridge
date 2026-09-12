import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from openai import OpenAI
from claude_native_bridge.api_provider import make_profile
from claude_native_bridge.models import MODELS


class APIProviderTests(unittest.TestCase):
    def test_standard_sdk_client_and_distinct_owner_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "config.yaml").write_text(
                "claude_native_bridge_api:\n  port: 19876\n"
            )
            profile = make_profile(home)
            self.assertEqual(profile.auth_type, "api_key")
            self.assertEqual(profile.fallback_models, MODELS)
            with (
                patch(
                    "claude_native_bridge.api_provider.active_home", return_value=home
                ),
                patch("claude_native_bridge.api_service.ensure_server") as start,
            ):
                a = profile.create_client(
                    api_key="x" * 40,
                    base_url=profile.base_url,
                    default_headers={"Existing": "keep"},
                    future_host_kwarg=True,
                )
                b = profile.create_client(api_key="x" * 40, base_url=profile.base_url)
                try:
                    self.assertIsInstance(a, OpenAI)
                    self.assertEqual(a.default_headers["Existing"], "keep")
                    self.assertNotEqual(
                        a.default_headers["X-Hermes-Bridge-Client"],
                        b.default_headers["X-Hermes-Bridge-Client"],
                    )
                    self.assertEqual(
                        profile.build_extra_body(session_id="s"),
                        {"hermes_session_id": "s"},
                    )
                    self.assertEqual(profile.build_extra_body(), {})
                    self.assertEqual(start.call_count, 2)
                finally:
                    a.close()
                    b.close()

    def test_bridge_key_is_not_sent_to_a_nonlocal_or_legacy_uri(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = make_profile(Path(folder))
            for url in ["https://example.invalid/v1", "claude-native://bridge"]:
                with (
                    self.subTest(url=url),
                    patch("claude_native_bridge.api_service.ensure_server") as start,
                ):
                    with self.assertRaises(ValueError):
                        profile.create_client(api_key="x" * 40, base_url=url)
                    start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
