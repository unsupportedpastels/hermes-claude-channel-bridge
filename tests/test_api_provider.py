import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai import OpenAI
from claude_native_bridge.api_provider import BridgeOpenAI, make_profile
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

    def test_client_close_releases_exact_api_owner_once(self):
        response = SimpleNamespace(close=lambda: None)
        with patch(
            "claude_native_bridge.api_provider.urlopen", return_value=response
        ) as send:
            client = BridgeOpenAI(
                api_key="x" * 40,
                base_url="http://127.0.0.1:19876/v1",
                bridge_close_url="http://127.0.0.1:19876/v1/owner/close",
                bridge_token="x" * 40,
                bridge_owner="owner-test",
            )
            client.close()
            client.close()
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:19876/v1/owner/close")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "x" * 40)
        self.assertEqual(request.get_header("X-hermes-bridge-client"), "owner-test")

    def test_failed_client_construction_releases_the_registered_owner(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("SDK extension could not load")

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "config.yaml").write_text(
                "claude_native_bridge_api:\n  port: 19876\n"
            )
            profile = make_profile(home)
            with (
                patch(
                    "claude_native_bridge.api_provider.active_home", return_value=home
                ),
                patch("claude_native_bridge.api_service.ensure_server") as start,
                patch(
                    "claude_native_bridge.api_provider._bridge_openai",
                    return_value=unavailable,
                ),
                patch(
                    "claude_native_bridge.api_provider.urlopen",
                    return_value=SimpleNamespace(close=lambda: None),
                ) as send,
            ):
                with self.assertRaises(RuntimeError):
                    profile.create_client(api_key="x" * 40, base_url=profile.base_url)
        owner = start.call_args.kwargs["client"]
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:19876/v1/owner/close")
        self.assertEqual(request.get_header("X-hermes-bridge-client"), owner)

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
