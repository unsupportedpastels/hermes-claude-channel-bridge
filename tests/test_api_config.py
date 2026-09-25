import os
from pathlib import Path
import tempfile
import unittest
from claude_native_bridge.api_config import api_storage, api_base_url, configured_port
from claude_native_bridge.api_server import configured_owner_limit
from claude_native_bridge.api_service import ensure_server


class APIConfigTests(unittest.TestCase):
    def test_port_validation_and_loopback_url(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "config.yaml").write_text(
                "claude_native_bridge_api:\n  port: 19765\n"
            )
            self.assertEqual(api_base_url(home), "http://127.0.0.1:19765/v1")
            for value in ["true", "0", "65536", '"19765"']:
                (home / "config.yaml").write_text(
                    "claude_native_bridge_api:\n  port: " + value + "\n"
                )
                with self.assertRaises(ValueError):
                    configured_port(home)

    @unittest.skipIf(
        os.name == "nt", "This fixture tests an existing Unix config symlink"
    )
    def test_shared_config_has_one_api_store_but_separate_env_files(self):
        with tempfile.TemporaryDirectory() as folder:
            a = Path(folder) / "cli"
            b = Path(folder) / "desktop"
            a.mkdir()
            b.mkdir()
            (a / "config.yaml").write_text("{}")
            (b / "config.yaml").symlink_to(a / "config.yaml")
            self.assertEqual(api_storage(a), api_storage(b))
            self.assertNotEqual(a / ".env", b / ".env")

    def test_missing_real_bridge_key_fails_before_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                ensure_server(Path(folder), "placeholder")
            self.assertFalse((Path(folder) / "claude-native-bridge").exists())

    def test_api_owner_limit_uses_plugin_max_sessions_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "config.yaml").write_text(
                "claude_native_bridge:\n  max_sessions: 10\n"
            )
            self.assertEqual(configured_owner_limit(home), 10)


if __name__ == "__main__":
    unittest.main()
