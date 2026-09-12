"""Plugin-owned setup and local API lifecycle commands."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(prog="python -m claude_native_bridge")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("setup", "start", "status", "stop"):
        command = commands.add_parser(action)
        command.add_argument("--home", type=Path)
        if action == "setup":
            command.add_argument("--accept-development-channels", action="store_true")
    args = parser.parse_args()
    if args.home is not None:
        os.environ["HERMES_HOME"] = str(args.home.resolve())
    from .api_config import active_home, api_storage, configured_port, api_base_url
    from .api_service import ensure_server, setup, stop_server, _health

    home = args.home.resolve() if args.home is not None else active_home()
    if args.action == "setup":
        result = setup(
            home, accept_development_channels=args.accept_development_channels
        )
    elif args.action == "stop":
        result = stop_server(home)
    else:
        keyfile = api_storage(home) / "token"
        if not keyfile.exists():
            raise SystemExit("Run plugin setup first; no local API credential exists")
        token = keyfile.read_text().strip()
        result = (
            ensure_server(home, token)
            if args.action == "start"
            else {
                "running": _health(configured_port(home), token),
                "base_url": api_base_url(home),
            }
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
