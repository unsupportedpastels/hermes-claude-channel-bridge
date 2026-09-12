"""Loopback-only API service entry point (no process startup at import)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import tempfile

import yaml

from .settings import Settings
from .supervisor import sweep_orphaned_runs


def configured_owner_limit(home):
    path = Path(home) / "config.yaml"
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Settings.from_mapping((raw or {}).get("claude_native_bridge", {})).max_sessions


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local authenticated Claude bridge API"
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.token_file.resolve() == args.ready_file.resolve():
        parser.error("token and readiness files must be distinct")
    if args.token_file.stat().st_size > 4096:
        parser.error("credential file is too large")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        parser.error("credential file is empty")

    sweep_orphaned_runs(args.home)

    import uvicorn
    from .api import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.unlink(missing_ok=True)

    class Server(uvicorn.Server):
        async def shutdown(self, sockets=None):
            try:
                await super().shutdown(sockets=sockets)
            finally:
                # Uvicorn re-raises termination signals before run() returns.
                args.ready_file.unlink(missing_ok=True)

        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                receipt = {
                    "pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": port,
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "health_url": f"http://127.0.0.1:{port}/health",
                }
                fd, name = tempfile.mkstemp(
                    prefix=".bridge-ready-", dir=args.ready_file.parent
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(receipt, handle)
                    os.replace(name, args.ready_file)
                finally:
                    Path(name).unlink(missing_ok=True)

    try:
        config = uvicorn.Config(
            create_app(
                token,
                args.home,
                owner_limit=configured_owner_limit(args.home),
            ),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            ws="none",
            timeout_graceful_shutdown=5,
        )
        Server(config).run(sockets=[sock])
    finally:
        sock.close()
        args.ready_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
