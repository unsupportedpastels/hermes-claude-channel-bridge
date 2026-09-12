"""A dedicated interactive native session. No vendor HTTP or credential transport."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

from .models import MODELS, reasoning_efforts
from .native_hooks import stopped_text
from .settings import NativeBridgeError, Settings
from .streaming import TextBatches
from .supervisor import process_start
from .usage import usage_for_request
from .platform_support import native_environment, script_command


class NativeSessionLost(NativeBridgeError):
    """The native process disappeared; retry only as a fresh canonical bootstrap."""


def child_environment(source=None):
    return native_environment(source)


def native_argv(command, session_id, mcp_path, model, effort):
    return [
        command,
        "--model",
        model,
        *(["--effort", effort] if reasoning_efforts(model) else []),
        "--session-id",
        session_id,
        "--tools",
        "",
        "--allowedTools",
        "mcp__hermesbridge__respond",
        "mcp__hermesbridge__read_result",
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_path),
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--prompt-suggestions",
        "false",
        "--dangerously-load-development-channels",
        "server:hermesbridge",
    ]


def consent_key(screen, runtime):
    selected = next(
        (line.strip() for line in screen.splitlines() if line.strip().startswith("❯")),
        "",
    )
    if "Accessing workspace:" in screen and str(runtime) in screen:
        if "No, exit" in selected:
            return ("trust", "Down")
        if "Yes, I trust this folder" in selected:
            return ("trust", "Enter")
    if (
        "WARNING: Loading development channels" in screen
        and "server:hermesbridge" in screen
        and "I am using this for local development" in selected
    ):
        return ("channel", "Enter")
    return None


class NativeSession:
    def __init__(
        self, settings: Settings, home: Path, model: str, effort: str, http_client=None
    ):
        self.settings = settings
        self.home = Path(home)
        self.model = model
        self.effort = effort
        self.session_id = str(uuid.uuid4())
        self.hermes_binding = None
        self.last_response_source = "respond"
        self.last_usage = None
        self.last_text = ""
        self._text_messages = set()
        self.socket_name = "hcb-" + self.session_id
        self.runtime = None
        self.port = None
        self.sequence = 0
        self.token = None
        self.closed = False
        self._lock = threading.RLock()
        self._request_active = False
        self._last_used = time.monotonic()
        self._janitor_stop = threading.Event()
        self._windows_controller = None
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(trust_env=False)

    def _tmux(self, *args, check=True):
        if sys.platform == "win32":
            if self.runtime is None:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if self._windows_controller is None:
                from .windows_controller import WindowsController

                self._windows_controller = WindowsController(self.runtime)
            return self._windows_controller.command(*args, check=check)
        return subprocess.run(
            ["tmux", "-L", self.socket_name, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=check,
        )

    def _private_json(self, name, value):
        target = self.runtime / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
        os.replace(temporary, target)

    def _heartbeat(self):
        self._private_json(
            "lease.json", {"expires": time.time() + self.settings.request_timeout + 30}
        )

    def start(self):
        self.settings.check_consent()
        if sys.platform not in ("linux", "darwin", "win32"):
            raise NativeBridgeError(
                "Native launcher supports Windows, macOS and Linux."
            )
        if self.model not in MODELS:
            raise NativeBridgeError("Unverified native model: " + self.model)
        command = shutil.which(self.settings.command)
        if (
            command is None
            or (sys.platform != "win32" and shutil.which("tmux") is None)
            or shutil.which("node") is None
        ):
            raise NativeBridgeError(
                "Install native Claude Code and Node; Linux/macOS also require tmux."
            )
        command = str(Path(command).resolve())
        server = Path(__file__).resolve().parent / "channel" / "server.mjs"
        dependencies = server.parent / "node_modules" / "@modelcontextprotocol" / "sdk"
        if not dependencies.is_dir():
            raise NativeBridgeError(
                "Missing channel dependencies; run npm ci --ignore-scripts in claude_native_bridge/channel."
            )
        env = child_environment()
        auth = subprocess.run(
            [command, "auth", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            status = json.loads(auth.stdout)
        except (json.JSONDecodeError, TypeError):
            raise NativeBridgeError(
                "Could not verify native Claude login. Run claude auth status yourself."
            ) from None
        if (
            auth.returncode
            or not status.get("loggedIn")
            or status.get("authMethod") != "claude.ai"
        ):
            raise NativeBridgeError(
                "A native claude.ai login is required. Run claude auth login; no API-key fallback is allowed."
            )
        parent = self.home / "claude-native-bridge" / "runs"
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime = Path(tempfile.mkdtemp(prefix="session-", dir=parent))
        if sys.platform == "win32":
            from .windows_security import secure_runtime_directory

            secure_runtime_directory(self.runtime)
        else:
            os.chmod(self.runtime, 0o700)
        self.token = secrets.token_hex(32)
        self._private_json("transport.json", {"token": self.token})
        hard_timeout = int(
            (self.settings.request_timeout + self.settings.idle_timeout + 60) * 1000
        )
        self._private_json(
            "mcp.json",
            {
                "mcpServers": {
                    "hermesbridge": {
                        "command": shutil.which("node"),
                        "args": [str(server)],
                        "env": {"HERMES_BRIDGE_RUNTIME_DIR": str(self.runtime)},
                        "timeout": hard_timeout,
                    }
                }
            },
        )
        args = native_argv(
            command, self.session_id, self.runtime / "mcp.json", self.model, self.effort
        )
        hook_command = script_command(
            sys.executable, Path(__file__).with_name("native_hooks.py"), self.runtime
        )
        hook = {"hooks": [{"type": "command", "command": hook_command, "timeout": 5}]}
        status_command = script_command(
            sys.executable, Path(__file__).with_name("usage.py"), self.runtime
        )
        self._private_json(
            "hooks.json",
            {
                "hooks": {
                    "Stop": [hook],
                    "StopFailure": [hook],
                    "MessageDisplay": [hook],
                },
                "statusLine": {"type": "command", "command": status_command},
            },
        )
        args.extend(["--settings", str(self.runtime / "hooks.json")])
        self._private_json(
            "launch.json",
            {
                "argv": args,
                "environment": env,
                "owner_pid": os.getpid(),
                "owner_start": process_start(os.getpid()),
                "session_id": self.session_id,
                "hermes_session_id": self.hermes_binding,
            },
        )
        self._heartbeat()
        supervisor = Path(__file__).with_name("supervisor.py")
        launch = shlex.join([sys.executable, str(supervisor), str(self.runtime)])
        consent = False
        last_key_time = 0
        consent_attempts = 0
        try:
            with self._lock:
                if self.closed:
                    raise NativeBridgeError("Native startup cancelled")
                self._tmux(
                    "new-session",
                    "-d",
                    "-s",
                    "worker",
                    "-x",
                    "160",
                    "-y",
                    "45",
                    "-c",
                    str(self.runtime),
                    launch,
                )
            deadline = time.monotonic() + self.settings.startup_timeout
            while time.monotonic() < deadline:
                if self.closed:
                    raise NativeBridgeError("Native startup cancelled")
                if self._tmux("has-session", "-t", "worker", check=False).returncode:
                    raise NativeBridgeError(
                        "Native Claude exited during startup. Inspect the dedicated runtime diagnostics."
                    )
                screen = self._tmux("capture-pane", "-t", "worker", "-p").stdout
                action = consent_key(screen, self.runtime)
                if action and time.monotonic() - last_key_time >= 0.6:
                    if consent_attempts >= 12:
                        raise NativeBridgeError(
                            "Native consent UI did not accept the expected input; stopping."
                        )
                    self._tmux("send-keys", "-t", "worker", action[1])
                    last_key_time = time.monotonic()
                    consent_attempts += 1
                    if action[0] == "channel":
                        consent = True
                ready = self.runtime / "ready.json"
                if consent and ready.exists():
                    data = json.loads(ready.read_text())
                    port = data.get("port")
                    if type(port) is not int or not 1 <= port <= 65535:
                        raise NativeBridgeError("Invalid local bridge port")
                    self.port = port
                    self._api("/status")
                    self._last_used = time.monotonic()
                    threading.Thread(
                        target=self._idle_watch,
                        name="hcb-idle-" + self.session_id,
                        daemon=True,
                    ).start()
                    return self
                time.sleep(0.1)
            if self.settings.retain_diagnostics:
                (self.runtime / "startup-screen.txt").write_text(screen)
            raise NativeBridgeError(
                "Native startup timed out; no inference was submitted. Check login or consent in the CLI."
            )
        except BaseException:
            self.close()
            raise

    def _api(self, endpoint, payload=None, timeout=12):
        if self.port is None or self.closed:
            raise NativeBridgeError("Native bridge is closed")
        try:
            r = self.http_client.request(
                "POST" if payload is not None else "GET",
                "http://127.0.0.1:" + str(self.port) + endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + self.token},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise NativeBridgeError(
                "Native bridge transport failed; response state is uncertain and will not be replayed automatically."
            ) from exc

    def health(self):
        """Return whether the dedicated native terminal is still alive."""
        if self.closed or self.runtime is None or self.port is None:
            return False
        try:
            return (
                self._tmux("has-session", "-t", "worker", check=False).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            return False

    def _usage_snapshot(self):
        try:
            return json.loads((self.runtime / "native-usage.json").read_text())
        except (OSError, ValueError):
            return None

    def _collect_usage(self, baseline, started_ns, cancel_check=None):
        if type(baseline) is not int:
            return
        # Native status-line updates are debounced by 300ms. Bound the wait;
        # missing/ambiguous telemetry must not fabricate usage or stall a turn.
        deadline = time.monotonic() + 1.5
        while not self.closed and time.monotonic() < deadline:
            if cancel_check and cancel_check():
                return
            snapshot = self._usage_snapshot()
            if snapshot and snapshot.get("captured_ns", 0) >= started_ns:
                usage = usage_for_request(
                    snapshot, self.session_id, self.model, baseline
                )
                if usage is not None:
                    self.last_usage = usage
                    return
                count = (snapshot.get("prompt_cache") or {}).get("requests")
                if type(count) is int and count > baseline + 1:
                    return
            time.sleep(0.025)

    def _check_stop_failure(self, request_id):
        path = self.runtime / "native-stop.json"
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("event") != "Stop" or record.get("background_pending"):
                stopped_text(record, request_id, self.session_id)

    def exchange(self, content, request_id, cancel_check=None, on_text=None):
        """Call synchronous on_text(str) serially with actual native display deltas.

        Callback exceptions abort this session. Stop reconciles plain-text finals;
        display-final alone is not end-of-turn. Returned text preserves every delta.
        """
        with self._lock:
            if self.closed:
                raise NativeBridgeError(
                    "Native session expired or closed; rebuild from canonical history."
                )
            self._request_active = True
        self.last_usage = None
        self.last_text = ""
        self.last_response_source = "respond"
        text_batches = TextBatches(
            self.session_id, request_id, on_text, self._text_messages
        )
        prior = self._usage_snapshot()
        baseline = (
            0
            if self.sequence == 0
            else ((prior or {}).get("prompt_cache") or {}).get("requests")
        )
        started_ns = time.monotonic_ns()
        try:
            if len(self._text_messages) >= 16384:
                raise NativeBridgeError("Native text message budget exhausted")
            self._heartbeat()
            for name in ("native-stop.json", "native-text.jsonl", "native-text-error"):
                (self.runtime / name).unlink(missing_ok=True)
            self._private_json(
                "active-request.json",
                {"session_id": self.session_id, "request_id": request_id},
            )
            self._api(
                "/advance",
                {
                    "ack": self.sequence or None,
                    "request": {"request_id": request_id, "content": content},
                },
            )
            deadline = time.monotonic() + self.settings.request_timeout
            while time.monotonic() < deadline:
                if self.closed:
                    raise NativeBridgeError("Native session cancelled")
                if cancel_check and cancel_check():
                    raise InterruptedError("Hermes interrupted native inference")
                text_batches.drain(self.runtime)
                stop_file = self.runtime / "native-stop.json"
                if stop_file.exists():
                    try:
                        text = stopped_text(
                            json.loads(stop_file.read_text()),
                            request_id,
                            self.session_id,
                        )
                    except ValueError as exc:
                        raise NativeBridgeError(str(exc)) from exc
                    text_batches.drain(self.runtime)
                    text = text_batches.finish(text)
                    result = self._api(
                        "/text-complete", {"request_id": request_id, "text": text}
                    )
                    response = result.get("response")
                    if response != {
                        "sequence": self.sequence + 1,
                        "request_id": request_id,
                        "kind": "final",
                        "text": text,
                    }:
                        raise NativeBridgeError(
                            "Native text completion correlation failed"
                        )
                    self.last_response_source = "native_stop"
                    self.last_text = text
                    self.sequence = response["sequence"]
                    self._collect_usage(baseline, started_ns, cancel_check)
                    self._check_stop_failure(request_id)
                    if self.closed or (cancel_check and cancel_check()):
                        raise InterruptedError("Hermes interrupted native inference")
                    return response
                result = self._api(
                    "/response?after=" + str(self.sequence) + "&wait_ms=500", timeout=3
                )
                response = result.get("response")
                if response is not None:
                    if (
                        response.get("sequence") != self.sequence + 1
                        or response.get("request_id") != request_id
                    ):
                        raise NativeBridgeError(
                            "Native bridge response correlation failed"
                        )
                    text_batches.drain(self.runtime)
                    # A display-final closes a message, not the native turn: respond
                    # can follow it. Never dispatch partial or inferred tool calls.
                    self.last_text = text_batches.finish()
                    if response.get("kind") == "final" and self.last_text:
                        response["text"] = text_batches.finish(response.get("text"))
                    self.sequence = response["sequence"]
                    self._collect_usage(baseline, started_ns, cancel_check)
                    self._check_stop_failure(request_id)
                    if self.closed or (cancel_check and cancel_check()):
                        raise InterruptedError("Hermes interrupted native inference")
                    return response
                status = self._api("/status")
                if status.get("failed"):
                    raise NativeBridgeError("Native channel cancelled or failed")
                self._heartbeat()
            raise TimeoutError(
                "Native inference timed out; the dedicated session was stopped."
            )
        except BaseException as exc:
            session_lost = (
                isinstance(exc, NativeBridgeError)
                and not self.closed
                and not self.health()
            )
            self.close()
            if session_lost:
                raise NativeSessionLost(
                    "Native session was lost; retry to rebuild from canonical history. "
                    "The uncertain in-flight request was not replayed."
                ) from exc
            raise
        finally:
            with self._lock:
                self._text_messages.update(text_batches.messages)
                self._request_active = False
                self._last_used = time.monotonic()

    def _idle_watch(self):
        while not self._janitor_stop.wait(0.5):
            with self._lock:
                idle = (
                    not self._request_active
                    and time.monotonic() - self._last_used > self.settings.idle_timeout
                )
            if idle:
                self.close()
                return

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._janitor_stop.set()
        native_pid = None
        if self.runtime and (self.runtime / "native-pid.json").exists():
            native_pid = json.loads((self.runtime / "native-pid.json").read_text()).get(
                "pid"
            )
        identity = process_start(native_pid) if type(native_pid) is int else None
        self._tmux("kill-server", check=False)
        if identity is not None:
            for sig, seconds in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
                if process_start(native_pid) != identity:
                    break
                try:
                    os.killpg(native_pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + seconds
                while (
                    process_start(native_pid) == identity
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
        if self._owns_http_client:
            self.http_client.close()
        if self.runtime and not self.settings.retain_diagnostics:
            shutil.rmtree(self.runtime, ignore_errors=True)
