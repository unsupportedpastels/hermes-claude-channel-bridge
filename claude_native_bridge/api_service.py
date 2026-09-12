"""Supervise the plugin's local HTTP service without changing Hermes core."""

from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from filelock import FileLock
import httpx
import yaml
import psutil

from .api_config import TOKEN_ENV, api_storage, configured_port


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


def _health(port, token):
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    try:
        with httpx.Client(trust_env=False, timeout=0.4) as client:
            response = client.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": "Bearer " + token},
            )
        return (
            response.status_code == 200
            and response.json().get("service") == "claude-native-bridge"
        )
    except (httpx.HTTPError, ValueError):
        return False


def ensure_server(home, token, *, port=None):
    """Start/reuse only our authenticated API; no native model is launched here."""
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
        if desired and _health(desired, token):
            return {
                "port": desired,
                "pid": previous.get("pid"),
                "base_url": f"http://127.0.0.1:{desired}/v1",
            }
        if not desired and previous.get("port") and _health(previous["port"], token):
            return {**previous, "base_url": f"http://127.0.0.1:{previous['port']}/v1"}
        keyfile = root / "token"
        if keyfile.exists() and not secrets.compare_digest(
            keyfile.read_text().strip(), token
        ):
            raise RuntimeError(
                "Bridge credential differs from stored local API credential; rerun setup rather than replacing a running account"
            )
        _write_private(keyfile, token)
        ready.unlink(missing_ok=True)
        package_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        env["PYTHONPATH"] = str(package_root)
        env["PYTHONUTF8"] = "1"
        argv = [
            sys.executable,
            "-m",
            "claude_native_bridge.api_server",
            "--home",
            str(home),
            "--token-file",
            str(keyfile),
            "--ready-file",
            str(ready),
            "--port",
            str(desired),
        ]
        kwargs = {"cwd": str(package_root), "env": env, "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log = root / "api.log"
        with log.open("ab") as output:
            process = subprocess.Popen(
                argv, stdout=output, stderr=subprocess.STDOUT, **kwargs
            )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Local API exited during startup; inspect {log}"
                    )
                if ready.exists():
                    info = json.loads(ready.read_text())
                    if info.get("pid") == process.pid and _health(
                        info.get("port"), token
                    ):
                        _write_private(
                            root / "manager.json",
                            json.dumps(
                                {
                                    "pid": process.pid,
                                    "created": psutil.Process(
                                        process.pid
                                    ).create_time(),
                                    "argv": argv,
                                }
                            ),
                        )
                        return {
                            **info,
                            "base_url": f"http://127.0.0.1:{info['port']}/v1",
                        }
                time.sleep(0.1)
            raise TimeoutError(f"Local API startup timed out; inspect {log}")
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            raise


def stop_server(home):
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
        try:
            process = psutil.Process(info["pid"])
            if (
                process.create_time() != info["created"]
                or process.cmdline() != info["argv"]
            ):
                raise RuntimeError(
                    "Recorded API process identity changed; refusing to stop it"
                )
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
        return {"stopped": True}


def setup(home, *, accept_development_channels=False):
    """Configure the active profile through Hermes' existing config writers."""
    home = Path(home).resolve()
    os.environ["HERMES_HOME"] = str(home)
    config_path = home / "config.yaml"
    before = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    root = _private_directory(api_storage(home))
    keyfile = root / "token"
    token = (
        keyfile.read_text().strip() if keyfile.exists() else secrets.token_urlsafe(40)
    )
    info = ensure_server(home, token, port=0)
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
        "credential_saved": True,
        "default_model_changed": False,
        "home": str(home),
    }
