import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_native_bridge.native_hooks import capture, open_request, wake_current


class _WakeHandler(BaseHTTPRequestHandler):
    received = None
    journal = None
    done = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length)),
            "journal": json.loads(type(self).journal.read_text().splitlines()[-1]),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')
        type(self).done.set()

    def log_message(self, *_args):
        pass


def _display(root, delta="now"):
    return capture(
        root,
        {
            "session_id": "session",
            "prompt_id": "prompt",
            "hook_event_name": "MessageDisplay",
            "turn_id": "turn",
            "message_id": "message",
            "index": 0,
            "final": False,
            "delta": delta,
        },
    )


def _runtime(root, port):
    (root / "transport.json").write_text(json.dumps({"token": "wake-secret"}))
    (root / "ready.json").write_text(json.dumps({"port": port, "pid": 1}))
    open_request(root, "session", "request")
    assert capture(
        root,
        {
            "session_id": "session",
            "prompt_id": "prompt",
            "hook_event_name": "UserPromptSubmit",
        },
    )


def test_display_journal_is_canonical_before_authenticated_loopback_wake(tmp_path):
    done = threading.Event()
    _WakeHandler.received = None
    _WakeHandler.journal = tmp_path / "native-text.jsonl"
    _WakeHandler.done = done
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _runtime(tmp_path, server.server_port)
        started = time.monotonic()
        assert _display(tmp_path)
        assert done.wait(1)
        elapsed = time.monotonic() - started
        wake = _WakeHandler.received
        assert wake["path"] == "/wake"
        assert wake["authorization"] == "Bearer wake-secret"
        assert wake["body"] == {
            "request_id": "request",
            "prompt_id": "prompt",
            "event": "MessageDisplay",
            "generation": 1,
        }
        assert wake["journal"]["delta"] == "now"
        assert wake["journal"]["generation"] == 1
        assert elapsed < 0.5
        done.clear()
        assert wake_current(tmp_path)
        assert done.wait(1)
        assert _WakeHandler.received["body"] == {
            "request_id": "request",
            "prompt_id": "prompt",
            "event": "Usage",
            "generation": 2,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_lost_wake_is_best_effort_and_journal_remains_recoverable(tmp_path):
    _runtime(tmp_path, 1)
    started = time.monotonic()
    assert _display(tmp_path, "recover me")
    elapsed = time.monotonic() - started
    record = json.loads((tmp_path / "native-text.jsonl").read_text())
    state = json.loads((tmp_path / "active-request.json").read_text())
    assert record["delta"] == "recover me"
    assert record["generation"] == 1
    assert state["wake_generation"] == 1
    assert not (tmp_path / "native-attribution-error").exists()
    assert elapsed < 0.5


def test_stop_uses_next_generation_and_keeps_prompt_correlation(tmp_path):
    _runtime(tmp_path, 1)
    assert _display(tmp_path)
    assert capture(
        tmp_path,
        {
            "session_id": "session",
            "prompt_id": "prompt",
            "hook_event_name": "Stop",
            "last_assistant_message": "done",
        },
    )
    stop = json.loads((tmp_path / "native-stop.json").read_text())
    assert stop["request_id"] == "request"
    assert stop["prompt_id"] == "prompt"
    assert stop["generation"] == 2


def test_wake_generation_budget_fails_closed(tmp_path):
    _runtime(tmp_path, 1)
    state_path = tmp_path / "active-request.json"
    state = json.loads(state_path.read_text())
    state["wake_generation"] = 65_536
    state_path.write_text(json.dumps(state))
    assert not _display(tmp_path)
    assert (tmp_path / "native-attribution-error").exists()
    assert not (tmp_path / "native-text.jsonl").exists()
