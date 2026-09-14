"""Plugin-owned setup and local API lifecycle commands.

Installed as the ``hermes-claude-bridge`` console script and runnable as
``python -m claude_native_bridge``. Hermes' plugin installer registers the
manifest but never runs npm or plugin setup, so ``setup`` owns both.
"""

import argparse
import json
import os
from pathlib import Path

PROG = "hermes-claude-bridge"


def _ensure_channel_dependencies(*, skip_install: bool) -> dict:
    from .channel_install import (
        channel_dependencies_ready,
        install_channel_dependencies,
        install_command,
    )

    if channel_dependencies_ready():
        return {"ready": True, "installed": False}
    if skip_install:
        raise SystemExit(
            "Channel dependencies are missing; run "
            + " ".join(install_command())
            + " or rerun setup without --skip-channel-install"
        )
    install_channel_dependencies()
    return {"ready": True, "installed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(prog=PROG)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("setup", "start", "status", "stop", "doctor"):
        command = commands.add_parser(action)
        command.add_argument("--home", type=Path)
        if action == "setup":
            command.add_argument("--accept-development-channels", action="store_true")
            command.add_argument(
                "--skip-channel-install",
                action="store_true",
                help="fail instead of running the locked npm ci when channel dependencies are missing",
            )
        elif action == "doctor":
            command.add_argument(
                "--check-cli-version",
                action="store_true",
                help="locally run the resolved Claude executable with --version",
            )
    args = parser.parse_args(argv)
    if args.home is not None:
        os.environ["HERMES_HOME"] = str(args.home.resolve())
    from .api_config import active_home

    home = args.home.resolve() if args.home is not None else active_home()
    if args.action == "doctor":
        from .diagnostics import doctor

        result = doctor(home, check_cli_version=args.check_cli_version)
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1

    # Lifecycle modules are deliberately not imported by the offline doctor.
    from .api_config import api_base_url, api_storage, configured_port
    from .api_service import _health, ensure_server, setup, stop_server

    if args.action == "setup":
        channel = _ensure_channel_dependencies(
            skip_install=args.skip_channel_install
        )
        result = setup(
            home, accept_development_channels=args.accept_development_channels
        )
        result["channel_dependencies"] = channel
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
