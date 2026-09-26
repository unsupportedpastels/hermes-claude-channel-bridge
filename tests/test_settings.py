import unittest
from claude_native_bridge.settings import Settings, NativeBridgeError


class SettingsTests(unittest.TestCase):
    def test_development_channel_requires_explicit_consent(self):
        with self.assertRaisesRegex(NativeBridgeError, "development_channels_accepted"):
            Settings.from_mapping({}).check_consent()
        Settings.from_mapping({"development_channels_accepted": True}).check_consent()

    def test_invalid_limits_and_unknown_keys_fail_loudly(self):
        for config in [
            {"idle_timeout": 0},
            {"request_timeout": float("inf")},
            {"stall_timeout": 0},
            {"max_sessions": 0},
            {"max_sessions": 11},
            {"development_channels_accepted": "true"},
            {"bogus": True},
        ]:
            with self.subTest(config=config), self.assertRaises(NativeBridgeError):
                Settings.from_mapping(config)

    def test_ten_concurrent_sessions_allowed(self):
        self.assertEqual(Settings.from_mapping({"max_sessions": 10}).max_sessions, 10)

    def test_settings_default_does_not_select_model_or_enable_fallback(self):
        s = Settings.from_mapping({"development_channels_accepted": True})
        self.assertEqual(s.effort, "medium")
        self.assertGreater(s.request_timeout, s.startup_timeout)


if __name__ == "__main__":
    unittest.main()
