import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_native_bridge import channel_install
from claude_native_bridge.__main__ import main
from claude_native_bridge.api_config import api_storage
from claude_native_bridge.api_provider import make_profile
from claude_native_bridge.diagnostics import CHANNEL_DEPENDENCIES
from claude_native_bridge.settings import NativeBridgeError


def _channel(tmp_path: Path, *, installed: bool) -> Path:
    root = tmp_path / "channel"
    root.mkdir()
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    if installed:
        _populate(root)
    return root


def _populate(root: Path) -> None:
    for name, version in CHANNEL_DEPENDENCIES.items():
        manifest = root / "node_modules" / Path(*name.split("/")) / "package.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"version": version}), encoding="utf-8")


def test_readiness_follows_the_pinned_doctor_contract(tmp_path):
    root = _channel(tmp_path, installed=True)
    assert channel_install.channel_dependencies_ready(root) is True
    zod = root / "node_modules" / "zod" / "package.json"
    zod.write_text(json.dumps({"version": "0.0.1"}), encoding="utf-8")
    assert channel_install.channel_dependencies_ready(root) is False


def test_install_runs_only_the_locked_npm_ci_and_verifies(tmp_path):
    root = _channel(tmp_path, installed=False)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        _populate(root)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = channel_install.install_channel_dependencies(
        root, executable_resolver=lambda name: "/fake/npm", runner=run
    )

    assert result["installed"] is True
    assert calls[0][0] == [
        "/fake/npm",
        "--prefix",
        str(root),
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    assert calls[0][1]["cwd"] == str(root)
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["check"] is False


def test_install_failures_name_the_manual_command(tmp_path):
    root = _channel(tmp_path, installed=False)
    failed = subprocess.CompletedProcess([], 7, stdout="", stderr="boom")
    with pytest.raises(NativeBridgeError, match="npm was not found"):
        channel_install.install_channel_dependencies(
            root, executable_resolver=lambda name: None, runner=lambda *a, **k: failed
        )

    with pytest.raises(NativeBridgeError, match="status 7") as caught:
        channel_install.install_channel_dependencies(
            root,
            executable_resolver=lambda name: "/fake/npm",
            runner=lambda *a, **k: failed,
        )
    assert "--ignore-scripts" in str(caught.value)
    assert "boom" in str(caught.value)

    ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with pytest.raises(NativeBridgeError, match="still do not match"):
        channel_install.install_channel_dependencies(
            root, executable_resolver=lambda name: "/fake/npm", runner=lambda *a, **k: ok
        )

    (root / "package-lock.json").unlink()
    with pytest.raises(NativeBridgeError, match="package-lock.json is missing"):
        channel_install.install_channel_dependencies(
            root, executable_resolver=lambda name: "/fake/npm", runner=lambda *a, **k: ok
        )


def test_setup_installs_missing_channel_dependencies_before_starting(tmp_path):
    order = []
    with (
        patch(
            "claude_native_bridge.channel_install.channel_dependencies_ready",
            return_value=False,
        ),
        patch(
            "claude_native_bridge.channel_install.install_channel_dependencies",
            side_effect=lambda: order.append("npm"),
        ),
        patch(
            "claude_native_bridge.api_service.setup",
            side_effect=lambda home, **kw: order.append("setup") or {"home": str(home)},
        ),
    ):
        assert main(["setup", "--home", str(tmp_path)]) == 0
    assert order == ["npm", "setup"]


def test_setup_skip_flag_fails_instead_of_running_npm(tmp_path):
    with (
        patch(
            "claude_native_bridge.channel_install.channel_dependencies_ready",
            return_value=False,
        ),
        patch(
            "claude_native_bridge.channel_install.install_channel_dependencies"
        ) as install,
        patch("claude_native_bridge.api_service.setup") as setup,
    ):
        with pytest.raises(SystemExit, match="--ignore-scripts"):
            main(["setup", "--home", str(tmp_path), "--skip-channel-install"])
    install.assert_not_called()
    setup.assert_not_called()


def test_setup_does_not_run_npm_when_dependencies_are_present(tmp_path):
    with (
        patch(
            "claude_native_bridge.channel_install.channel_dependencies_ready",
            return_value=True,
        ),
        patch(
            "claude_native_bridge.channel_install.install_channel_dependencies"
        ) as install,
        patch("claude_native_bridge.api_service.setup", return_value={}),
    ):
        assert main(["setup", "--home", str(tmp_path)]) == 0
    install.assert_not_called()


def test_provider_first_run_points_at_setup_before_any_server_start(tmp_path):
    (tmp_path / "config.yaml").write_text("claude_native_bridge_api:\n  port: 19876\n")
    profile = make_profile(tmp_path)
    with (
        patch("claude_native_bridge.api_provider.active_home", return_value=tmp_path),
        patch("claude_native_bridge.api_service.ensure_server") as start,
        patch.dict("os.environ", {"CLAUDE_NATIVE_BRIDGE_API_KEY": ""}),
    ):
        with pytest.raises(ValueError, match="hermes-claude-bridge setup"):
            profile.create_client(api_key="", base_url=profile.base_url)
        with (
            patch(
                "claude_native_bridge.channel_install.channel_dependencies_ready",
                return_value=False,
            ),
            pytest.raises(ValueError, match="development_channels_accepted"),
        ):
            profile.create_client(api_key="x" * 40, base_url=profile.base_url)
    start.assert_not_called()


def test_provider_runs_setup_on_first_use_when_consent_is_recorded(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "claude_native_bridge_api:\n  port: 19876\n"
        "claude_native_bridge:\n  development_channels_accepted: true\n"
    )
    keyfile = api_storage(tmp_path) / "token"
    order = []

    def fake_setup(home, *, accept_development_channels):
        assert accept_development_channels is True
        keyfile.parent.mkdir(parents=True)
        keyfile.write_text("t" * 40)
        order.append("setup")
        return {"base_url": "http://127.0.0.1:19999/v1", "pid": 1}

    profile = make_profile(tmp_path)
    with (
        patch("claude_native_bridge.api_provider.active_home", return_value=tmp_path),
        patch(
            "claude_native_bridge.channel_install.channel_dependencies_ready",
            return_value=False,
        ),
        patch(
            "claude_native_bridge.channel_install.install_channel_dependencies",
            side_effect=lambda: order.append("npm"),
        ),
        patch("claude_native_bridge.api_service.setup", side_effect=fake_setup),
        patch("claude_native_bridge.api_service.ensure_server") as start,
        patch("claude_native_bridge.api_provider.BridgeOpenAI") as client,
        patch.dict("os.environ", {"CLAUDE_NATIVE_BRIDGE_API_KEY": ""}),
    ):
        profile.create_client(api_key="", base_url=profile.base_url)

    assert order == ["npm", "setup"]
    start.assert_called_once()
    assert start.call_args.kwargs["port"] == 19999
    assert client.call_args.kwargs["api_key"] == "t" * 40
    assert client.call_args.kwargs["base_url"] == "http://127.0.0.1:19999/v1"
