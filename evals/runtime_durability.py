"""Real-process durability receipts for the isolated server runtime.

Run this under a managed-host-like interpreter: a bare Python whose
dependencies reach sys.path only through ``--host-site`` (the way Hermes's
bootstrap activates its dependency generation), never through PYTHONPATH.
That is the condition in which the old ``sys.executable -m`` launch failed.

Everything happens in a throwaway HERMES_HOME. The Hermes package manager
builds the server runtimes under that home. No model call is made and no
native Claude session is started; health checks only.

    <bare-python> -I evals/runtime_durability.py \
        --host-site <dependency-generation site-packages> \
        --hermes-root <hermes-agent checkout>
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]

CLIENT = r"""
import json, os, sys, time
spec = json.loads(os.environ["CNB_EVAL"])
sys.path.insert(0, spec["root"])
sys.path += [spec["site"], spec["hermes"]]
from claude_native_bridge.api_service import ensure_server
print(json.dumps(ensure_server(spec["home"], spec["token"], port=0)), flush=True)
time.sleep(spec["linger"])
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-site", required=True)
    parser.add_argument("--hermes-root", required=True)
    parser.add_argument("--idle", type=int, default=15)
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    sys.path += [options.host_site, options.hermes_root]

    import httpx
    import psutil
    from claude_native_bridge import api_service
    from claude_native_bridge import runtime_environment as rt
    from claude_native_bridge.api_config import api_storage

    work = Path(tempfile.mkdtemp(prefix="cnb-durability-"))
    receipts = {}
    running = []

    def step(name, value):
        receipts[name] = value
        print(json.dumps({name: value}), flush=True)

    def home(name):
        path = work / name
        path.mkdir()
        (path / "config.yaml").write_text(
            f"claude_native_bridge_api:\n  idle_exit_seconds: {options.idle}\n"
        )
        return path

    def client(where, token, linger=0.0):
        spec = {
            "root": str(ROOT),
            "site": options.host_site,
            "hermes": options.hermes_root,
            "home": str(where),
            "token": token,
            "linger": linger,
        }
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", CLIENT],
            env={**os.environ, "CNB_EVAL": json.dumps(spec)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("client failed: " + process.stderr.read()[-2000:])
        info = json.loads(line)
        running.append(info["pid"])
        return process, info

    def start(where, token):
        # In-process start, for steps that override SERVER_REQUIREMENTS here;
        # a separate client would (correctly) see the runtime as outdated.
        info = api_service.ensure_server(where, token, port=0)
        running.append(info["pid"])
        return info

    def healthy(info, token):
        # No process header: the eval itself must not count as a live client.
        try:
            response = httpx.get(
                f"http://127.0.0.1:{info['port']}/health",
                headers={"Authorization": "Bearer " + token},
                timeout=2,
                trust_env=False,
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def wait_exit(pid, seconds):
        started = time.monotonic()
        try:
            psutil.Process(pid).wait(timeout=seconds)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            return None
        return round(time.monotonic() - started, 1)

    def server_files(pid):
        process = psutil.Process(pid)
        mapped = {m.path for m in process.memory_maps() if m.path}
        application = [
            path
            for path in mapped
            if path.startswith(str(Path(options.hermes_root) / "venv"))
            or path.startswith(str(Path(options.host_site).parents[2]))
        ]
        return {
            "exe": process.exe(),
            "argv0": process.cmdline()[0],
            "mapped_files": len(mapped),
            "application_env_files": sorted(application)[:5],
        }

    try:
        old = subprocess.run(
            [sys.executable, "-m", "claude_native_bridge.api_server", "--help"],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        step(
            "old_launch_contract",
            {"exit": old.returncode, "error": rt.error_summary(old.stderr)},
        )
        assert old.returncode != 0, "the bare host interpreter unexpectedly ran the server"

        home_a = home("home-a")
        token_a = secrets.token_urlsafe(40)
        started = time.monotonic()
        python_a = rt.provision(home_a)
        step(
            "provision",
            {
                "python": str(python_a),
                "seconds": round(time.monotonic() - started, 1),
                "status": rt.status(home_a),
            },
        )
        # The runtime may share the host's base interpreter binary (a venv
        # symlink); what must differ is the environment it runs in.
        assert Path(python_a).is_relative_to(rt.environment_root(home_a))
        probe = rt.probe(python_a)
        step("probe", probe)
        assert Path(probe["package"]) == ROOT / "claude_native_bridge"

        first, info = client(home_a, token_a)
        first.wait(timeout=30)
        files = server_files(info["pid"])
        step("server", {"pid": info["pid"], "port": info["port"], **files})
        assert healthy(info, token_a)
        assert files["argv0"] == str(python_a)
        assert not files["application_env_files"], files["application_env_files"]

        holder, _ = client(home_a, token_a, linger=options.idle * 4)
        time.sleep(options.idle + 12)
        kept = psutil.pid_exists(info["pid"]) and healthy(info, token_a)
        holder.kill()
        holder.wait()
        retired_after = wait_exit(info["pid"], options.idle + 30)
        events = (api_storage(home_a) / "events.jsonl").read_text().splitlines()
        retiring = [json.loads(e) for e in events if "service_retiring" in e]
        step(
            "idle_retirement",
            {
                "kept_alive_by_live_client": kept,
                "exited_seconds_after_client_death": retired_after,
                "journal": retiring[-1:],
            },
        )
        assert kept and retired_after is not None
        assert retiring and retiring[-1]["reason"] == "idle"

        _, again = client(home_a, token_a)
        assert again["pid"] != info["pid"] and healthy(again, token_a)
        stopped = api_service.stop_server(home_a)
        step(
            "cold_restart_then_graceful_stop",
            {
                "new_pid": again["pid"],
                "stop": stopped,
                "pid_gone": not psutil.pid_exists(again["pid"]),
                "port_released": not healthy(again, token_a),
            },
        )
        assert stopped["graceful"] and not stopped["forced"]

        original = rt.SERVER_REQUIREMENTS
        rt.SERVER_REQUIREMENTS = (*original, "packaging>=20")
        outdated = rt.status(home_a)["state"]
        python_b = rt.provision(home_a)
        upgraded = start(home_a, token_a)
        step(
            "upgrade_a_to_b",
            {
                "state_before": outdated,
                "python_b": str(python_b),
                "previous_still_present": Path(python_a).exists(),
                "server_argv0": server_files(upgraded["pid"])["argv0"],
            },
        )
        assert outdated == "outdated" and python_b != python_a
        assert server_files(upgraded["pid"])["argv0"] == str(python_b)
        api_service.stop_server(home_a)

        rt.SERVER_REQUIREMENTS = (*original, "claude-native-bridge-no-such-package>=1")
        try:
            rt.provision(home_a)
            failed = None
        except Exception as exc:
            failed = type(exc).__name__
        rt.SERVER_REQUIREMENTS = (*original, "packaging>=20")
        recovered = start(home_a, token_a)
        step(
            "failed_upgrade_keeps_previous",
            {
                "error": failed,
                "state": rt.status(home_a)["state"],
                "server_argv0": server_files(recovered["pid"])["argv0"],
            },
        )
        assert failed and rt.status(home_a)["state"] == "ready"
        assert server_files(recovered["pid"])["argv0"] == str(python_b)

        home_b = home("home-b")
        token_b = secrets.token_urlsafe(40)
        rt.provision(home_b)
        other = start(home_b, token_b)
        api_service.stop_server(home_a)
        step(
            "two_homes",
            {
                "ports": [recovered["port"], other["port"]],
                "runtimes_distinct": rt.environment_root(home_a) != rt.environment_root(home_b),
                "b_survives_a_stop": healthy(other, token_b),
                "a_token_rejected_by_b": not healthy(other, token_a),
            },
        )
        assert recovered["port"] != other["port"] and healthy(other, token_b)
        api_service.stop_server(home_b)
        rt.SERVER_REQUIREMENTS = original
        step("result", "pass")
    finally:
        for pid in running:
            try:
                process = psutil.Process(pid)
                if "claude_native_bridge.api_server" in process.cmdline():
                    process.terminate()
            except psutil.Error:
                pass
        if options.keep:
            print(json.dumps({"work": str(work)}))
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
