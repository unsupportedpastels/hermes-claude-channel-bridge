import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from claude_native_bridge.api_config import api_storage
from claude_native_bridge.api_service import stop_server


class ServiceStopTests(unittest.TestCase):
    def test_stop_holds_the_same_lock_through_process_and_metadata_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = api_storage(Path(folder))
            root.mkdir(parents=True)
            manager = root / "manager.json"
            ready = root / "server.json"
            manager.write_text(
                json.dumps({"pid": 123, "created": 1.0, "argv": ["fixture-api"]})
            )
            ready.write_text("{}")
            held = []
            case = self

            class Lock:
                def __init__(self, path, timeout):
                    case.assertEqual(path, str(root / "server.lock"))

                def __enter__(self):
                    held.append(True)

                def __exit__(self, *args):
                    case.assertFalse(manager.exists())
                    case.assertFalse(ready.exists())
                    held.clear()

            class Process:
                def create_time(self):
                    return 1.0

                def cmdline(self):
                    return ["fixture-api"]

                def terminate(self):
                    case.assertTrue(
                        held, "Stop must exclude concurrent service startup"
                    )

                def wait(self, timeout):
                    case.assertTrue(held)

            with (
                patch("claude_native_bridge.api_service.FileLock", Lock),
                patch(
                    "claude_native_bridge.api_service.psutil.Process",
                    return_value=Process(),
                ),
            ):
                self.assertTrue(stop_server(Path(folder))["stopped"])


if __name__ == "__main__":
    unittest.main()
