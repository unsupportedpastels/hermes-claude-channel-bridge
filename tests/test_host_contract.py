"""Exercise the real Hermes socket-abort path, without vendor inference."""

import http.server
from pathlib import Path
import tempfile
import threading
import unittest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings


class HostAbortTests(unittest.TestCase):
    def test_hermes_socket_abort_unwinds_and_closes_native_owner(self):
        from agent.agent_runtime_helpers import force_close_tcp_sockets

        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        errors = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set()
                release.wait(8)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()

        class WaitingNative:
            def __init__(self, settings, home, model, effort, http_client):
                self.http = http_client
                self.closed = False

            def start(self):
                return self

            def exchange(self, *args, **kwargs):
                self.http.get(
                    "http://127.0.0.1:" + str(server.server_port) + "/wait", timeout=8
                )
                raise AssertionError(
                    "An aborted request must not return a model response"
                )

            def close(self):
                self.closed = True
                closed.set()
                release.set()

        with tempfile.TemporaryDirectory() as home:
            c = NativeBridgeClient(
                hermes_home=Path(home),
                settings=Settings(development_channels_accepted=True),
                native_factory=WaitingNative,
            )

            def request():
                try:
                    c.chat.completions.create(
                        model="claude-sonnet-5",
                        messages=[{"role": "user", "content": "test"}],
                        extra_body={"hermes_session_id": "abort-test"},
                    )
                except BaseException as e:
                    errors.append(e)

            worker = threading.Thread(target=request, daemon=True)
            try:
                worker.start()
                self.assertTrue(entered.wait(5))
                self.assertGreaterEqual(force_close_tcp_sockets(c), 1)
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(closed.is_set())
                self.assertTrue(c.is_closed)
                self.assertTrue(errors)
            finally:
                c.close()
                release.set()
                server.shutdown()
                server.server_close()
                worker.join(5)


if __name__ == "__main__":
    unittest.main()
