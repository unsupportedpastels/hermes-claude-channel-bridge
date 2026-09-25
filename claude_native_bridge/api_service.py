"""Supervise the plugin's local HTTP service without changing Hermes core."""

from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from filelock import FileLock
import httpx
import yaml
import psutil

from .api_config import (
    PROCESS_HEADER,
    TOKEN_ENV,
    api_storage,
    configured_port,
    process_identity,
)
from .runtime_environment import (
    REPAIR_COMMAND,
    SERVER_MODULE,
    child_environment,
    command,
    ensure_ready,
    error_summary,
)

STARTUP_TIMEOUT_SECONDS = 20
DRAIN_WAIT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 30


def _private_directory(path):
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            from .windows_security import secure_runtime_directory

            secure_runtime_directory(path)
        else:
            path.mkdir(mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("API state directory must be a real private directory")
    if sys.platform != "win32":
        os.chmod(path, 0o700)
    return path


def _write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def _service_state(port, token):
    """``"ok"``, ``"draining"`` or None; also registers this process as a client."""
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    try:
        with httpx.Client(trust_env=False, timeout=0.4) as client:
            response = client.get(
                f"http://127.0.0.1:{port}/health",
                headers={
                    "Authorization": "Bearer " + token,
                    PROCESS_HEADER: process_identity(),
                },
            )
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("service") != "claude-native-bridge":
        return None
    if response.status_code == 200:
        return "ok"
    if response.status_code == 503 and body.get("status") == "draining":
        return "draining"
    return None


def _health(port, token):
    return _service_state(port, token) == "ok"


def _port_in_use(port):
    """True when something already accepts connections on the loopback port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_for_release(port, token):
    """A draining service still owns its port; wait for it instead of racing it."""
    deadline = time.monotonic() + DRAIN_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _service_state(port, token) != "draining":
            return
        time.sleep(0.2)
    raise RuntimeError("Local API is shutting down; retry shortly")


def _startup_failure(log, offset, status):
    try:
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read(65536).decode("utf-8", "replace")
    except OSError:
        text = ""
    detail = error_summary(text)
    hint = (
        f"; run {REPAIR_COMMAND}"
        if detail and detail.startswith(("ModuleNotFoundError", "ImportError"))
        else ""
    )
    return RuntimeError(
        f"Local API exited during startup (status {status}"
        + (f": {detail}" if detail else "")
        + f"){hint}; inspect {log}"
    )


def ensure_server(home, token, *, port=None, prepare_runtime=None):
    """Start/reuse only our authenticated API; no native model is launched here.

    ``prepare_runtime`` returns the server interpreter and runs only when a
    launch is needed; by default an unready runtime is reported, not built.
    """
    if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError(
            "A configured bridge-only API key is required; run plugin setup"
        )
    home = Path(home).resolve()
    root = _private_directory(api_storage(home))
    desired = configured_port(home) if port is None else port
    if type(desired) is not int or not 0 <= desired <= 65535:
        raise ValueError("Invalid local API port")
    ready = root / "server.json"
    with FileLock(str(root / "server.lock"), timeout=20):
        previous = json.loads(ready.read_text()) if ready.exists() else {}
        candidate = desired or previous.get("port")
        state = _service_state(candidate, token) if candidate else None
        if state == "ok":
            return {
                **(previous if not desired else {"pid": previous.get("pid")}),
                "port": candidate,
                "base_url": f"http://127.0.0.1:{candidate}/v1",
            }
        if state == "draining":
            _wait_for_release(candidate, token)
        if desired and _port_in_use(desired):
            # Not our service with this credential: typically the bridge of
            # another Hermes home configured with the same port. Launching
            # would only fail to bind after writing this home's state.
            raise RuntimeError(
                f"Local API port {desired} is already in use by another process "
                "(possibly the bridge of another Hermes home); give this home its "
                "own claude_native_bridge_api.port or use the home that owns it"
            )
        keyfile = root / "token"
        if keyfile.exists() and not secrets.compare_digest(
            keyfile.read_text().strip(), token
        ):
            raise RuntimeError(
                "Bridge credential differs from stored local API credential; rerun setup rather than replacing a running account"
            )
        python = prepare_runtime() if prepare_runtime else ensure_ready(home)
        _write_private(keyfile, token)
        ready.unlink(missing_ok=True)
        argv = command(
            python,
            SERVER_MODULE,
            "--home",
            home,
            "--token-file",
            keyfile,
            "--ready-file",
            ready,
            "--port",
            desired,
        )
        kwargs = {
            "cwd": str(root),
            "env": child_environment(HERMES_HOME=str(home)),
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log = root / "api.log"
        offset = log.stat().st_size if log.exists() else 0
        with log.open("ab") as output:
            process = subprocess.Popen(
                argv, stdout=output, stderr=subprocess.STDOUT, **kwargs
            )
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if ready.exists():
                    info = json.loads(ready.read_text())
                    try:
                        service = psutil.Process(info.get("pid"))
                        actual_argv = service.cmdline()
                    except (psutil.Error, TypeError):
                        service = None
                        actual_argv = []
                    if (
                        service is not None
                        and actual_argv[1:] == argv[1:]
                        and _health(info.get("port"), token)
                    ):
                        _write_private(
                            root / "manager.json",
                            json.dumps(
                                {
                                    "pid": service.pid,
                                    "created": service.create_time(),
                                    "argv": actual_argv,
                                    "python": str(python),
                                }
                            ),
                        )
                        return {
                            **info,
                            "base_url": f"http://127.0.0.1:{info['port']}/v1",
                        }
                status = process.poll()
                # A Windows venv python.exe is a redirector: it may exit zero
                # after spawning the real interpreter recorded in ready.json.
                if status is not None and not (sys.platform == "win32" and status == 0):
                    raise _startup_failure(log, offset, status)
                time.sleep(0.1)
            raise TimeoutError(f"Local API startup timed out; inspect {log}")
        except BaseException:
            if ready.exists():
                try:
                    service = psutil.Process(json.loads(ready.read_text())["pid"])
                    if service.cmdline()[1:] == argv[1:]:
                        service.terminate()
                        service.wait(timeout=3)
                except (psutil.Error, KeyError, ValueError, json.JSONDecodeError):
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            raise


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _request_shutdown(port, token):
    """Ask the service to drain and exit on its own; False if it cannot be asked."""
    if type(port) is not int or not token:
        return False
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/v1/service/shutdown",
                headers={"Authorization": "Bearer " + token},
            )
        return response.status_code == 202
    except httpx.HTTPError:
        return False


def _same_process(process, info):
    return process.create_time() == info["created"] and process.cmdline() == info["argv"]


def stop_server(home, *, timeout=SHUTDOWN_TIMEOUT_SECONDS):
    root = api_storage(home)
    if not root.exists():
        return {"stopped": False, "reason": "No managed API process recorded"}
    # Serialize against ensure_server so stopping one instance cannot delete
    # the readiness/identity records of a concurrently started replacement.
    with FileLock(str(root / "server.lock"), timeout=20):
        manager = root / "manager.json"
        if not manager.exists():
            return {"stopped": False, "reason": "No managed API process recorded"}
        info = json.loads(manager.read_text())
        result = {"stopped": True, "graceful": False, "forced": False}
        try:
            process = psutil.Process(info["pid"])
            if not _same_process(process, info):
                raise RuntimeError(
                    "Recorded API process identity changed; refusing to stop it"
                )
            token = (
                (root / "token").read_text().strip()
                if (root / "token").exists()
                else ""
            )
            port = _read_json(root / "server.json").get("port")
            if _request_shutdown(port, token):
                try:
                    process.wait(timeout=timeout)
                    result["graceful"] = True
                except psutil.TimeoutExpired:
                    pass
            # Re-check identity immediately before any forced termination.
            if not result["graceful"] and _same_process(process, info):
                result["forced"] = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except psutil.NoSuchProcess:
            pass
        manager.unlink(missing_ok=True)
        (root / "server.json").unlink(missing_ok=True)
        return result


def setup(home, *, accept_development_channels=False):
    """Configure the active profile through Hermes' existing config writers."""
    from .runtime_environment import provision

    home = Path(home).resolve()
    os.environ["HERMES_HOME"] = str(home)
    config_path = home / "config.yaml"
    before = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    root = _private_directory(api_storage(home))
    keyfile = root / "token"
    token = (
        keyfile.read_text().strip() if keyfile.exists() else secrets.token_urlsafe(40)
    )
    runtime = provision(home)
    info = ensure_server(home, token, port=0, prepare_runtime=lambda: runtime)
    from hermes_cli.config import save_config, save_env_value, get_env_path
    from dotenv import dotenv_values

    updates = {"claude_native_bridge_api": {"port": info["port"]}}
    if accept_development_channels:
        updates["claude_native_bridge"] = {"development_channels_accepted": True}
    save_config(updates, merge_existing=True)
    save_env_value(TOKEN_ENV, token)
    after = yaml.safe_load(config_path.read_text())
    if (after or {}).get("model") != (before or {}).get("model"):
        raise RuntimeError("Default-model preservation check failed during setup")
    if dotenv_values(get_env_path()).get(TOKEN_ENV) != token:
        raise RuntimeError(
            "Bridge API credential was not saved; check managed configuration policy"
        )
    return {
        "base_url": info["base_url"],
        "pid": info["pid"],
        "runtime": str(runtime),
        "credential_saved": True,
        "default_model_changed": False,
        "home": str(home),
    }
