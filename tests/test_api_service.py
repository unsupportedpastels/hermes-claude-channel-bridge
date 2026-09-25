import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest

from claude_native_bridge import api_service
from claude_native_bridge.api_config import api_storage
from claude_native_bridge.runtime_environment import RuntimeUnavailable
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


TOKEN = "t" * 40


class _FailingLaunch:
    """Stands in for Popen: records the launch and exits like a broken child."""

    calls = []

    def __init__(self, argv, **kwargs):
        _FailingLaunch.calls.append((argv, kwargs))
        kwargs["stdout"].write(
            b"Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'\n"
        )
        kwargs["stdout"].flush()

    def poll(self):
        return 1


@pytest.fixture
def launches(monkeypatch):
    _FailingLaunch.calls = []
    monkeypatch.setattr(api_service.subprocess, "Popen", _FailingLaunch)
    monkeypatch.setattr(api_service, "_service_state", lambda port, token, client=None: None)
    return _FailingLaunch.calls


def test_launch_uses_the_prepared_runtime_not_the_host_interpreter(
    tmp_path, monkeypatch, launches
):
    monkeypatch.setenv("PYTHONPATH", "/host/dependency/generation")
    runtime = tmp_path / "runtime" / "python"
    with pytest.raises(RuntimeError, match="exited during startup"):
        api_service.ensure_server(
            tmp_path, TOKEN, port=45678, prepare_runtime=lambda: runtime
        )
    argv, kwargs = launches[0]
    assert argv[0] == str(runtime) != sys.executable
    assert argv[1:5] == ["-I", "-X", "utf8", "-c"]
    assert "claude_native_bridge.api_server" in argv
    assert "PYTHONPATH" not in kwargs["env"]
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path.resolve())


def test_startup_failure_names_the_import_error_and_repair(tmp_path, launches):
    with pytest.raises(RuntimeError) as failure:
        api_service.ensure_server(
            tmp_path, TOKEN, port=45678, prepare_runtime=lambda: Path("/r/python")
        )
    message = str(failure.value)
    assert "ModuleNotFoundError: No module named 'yaml'" in message
    assert "hermes-claude-bridge repair" in message


def test_unready_runtime_is_reported_and_nothing_is_launched(tmp_path, launches):
    with pytest.raises(RuntimeUnavailable, match="hermes-claude-bridge repair"):
        api_service.ensure_server(tmp_path, TOKEN, port=45678)
    assert launches == []


def test_port_owned_by_another_process_is_reported_without_side_effects(
    tmp_path, launches
):
    import socket

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        with pytest.raises(RuntimeError, match=f"port {port} is already in use"):
            api_service.ensure_server(
                tmp_path,
                TOKEN,
                port=port,
                prepare_runtime=lambda: pytest.fail("must not prepare a runtime"),
            )
    assert launches == []
    assert not (api_storage(tmp_path) / "token").exists()


def test_stop_prefers_graceful_shutdown_over_termination(tmp_path, monkeypatch):
    root = api_storage(tmp_path)
    root.mkdir(parents=True)
    (root / "manager.json").write_text(
        json.dumps({"pid": 123, "created": 1.0, "argv": ["fixture-api"]})
    )
    (root / "server.json").write_text(json.dumps({"port": 45678}))
    (root / "token").write_text(TOKEN)
    events = []

    class Process:
        def create_time(self):
            return 1.0

        def cmdline(self):
            return ["fixture-api"]

        def wait(self, timeout):
            events.append(("wait", timeout))

        def terminate(self):
            events.append("terminate")

    monkeypatch.setattr(api_service.psutil, "Process", lambda pid: Process())
    monkeypatch.setattr(
        api_service,
        "_request_shutdown",
        lambda port, token: events.append(("shutdown", port, token == TOKEN)) or True,
    )
    result = stop_server(tmp_path)
    assert result == {"stopped": True, "graceful": True, "forced": False}
    assert events == [("shutdown", 45678, True), ("wait", 30)]
    assert not (root / "manager.json").exists()


if __name__ == "__main__":
    unittest.main()
