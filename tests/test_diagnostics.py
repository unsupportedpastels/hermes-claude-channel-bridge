import json
from pathlib import Path
import subprocess
import sys

from claude_native_bridge import diagnostics
from claude_native_bridge.diagnostics import doctor
from claude_native_bridge.__main__ import main


def _write_config(home: Path, section: dict) -> None:
    (home / "config.yaml").write_text(
        "claude_native_bridge:\n"
        + "\n".join(f"  {key}: {json.dumps(value)}" for key, value in section.items())
        + "\n",
        encoding="utf-8",
    )


def _versions(name: str) -> str:
    return {"PyYAML": "6.0.2"}[name]


def test_default_doctor_is_offline_and_leaves_cli_version_unknown(tmp_path):
    _write_config(
        tmp_path,
        {"command": "claude-custom", "development_channels_accepted": True},
    )
    calls = []

    report = doctor(
        tmp_path,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name=sys.platform,
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert calls == []
    assert report["offline"] is True
    assert report["version_probe_performed"] is False
    version = next(c for c in report["checks"] if c["id"] == "claude_cli_version")
    assert version["status"] == "warn"
    assert version["version"] is None
    assert version["compatibility"] == "unknown"


def test_explicit_version_probe_parses_only_version_and_marks_known_matrix(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})
    completed = subprocess.CompletedProcess([], 0, stdout="Claude Code 2.1.270\n", stderr="")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed

    report = doctor(
        tmp_path,
        check_cli_version=True,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name="linux",
        runner=run,
    )

    assert calls[0][0] == ["/safe/claude", "--version"]
    assert calls[0][1]["timeout"] == 5
    check = next(c for c in report["checks"] if c["id"] == "claude_cli_version")
    assert check == {
        "id": "claude_cli_version",
        "status": "pass",
        "version": "2.1.270",
        "compatibility": "known",
        "detail": "Verified for ordinary bridge operation on Linux; direct MCP paging has known CLI-sensitive limitations.",
    }


def test_unverified_cli_version_warns_without_failing_readiness(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})
    report = doctor(
        tmp_path,
        check_cli_version=True,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name="linux",
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, stdout="Claude Code 9.8.7 leaked-looking-extra-output", stderr=""
        ),
    )

    check = next(c for c in report["checks"] if c["id"] == "claude_cli_version")
    assert check["status"] == "warn"
    assert check["version"] == "9.8.7"
    assert check["compatibility"] == "unknown"
    assert "leaked-looking" not in json.dumps(report)
    assert report["ready"] is True


def test_required_dependency_and_executable_failures_make_report_not_ready(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})

    def version(name):
        raise LookupError(name)

    report = doctor(
        tmp_path,
        executable_resolver=lambda name: None if name in {"claude", "node"} else f"/bin/{name}",
        distribution_version=version,
        required_distributions=("missing-package",),
        platform_name="linux",
    )

    assert report["ready"] is False
    failures = {c["id"] for c in report["checks"] if c["status"] == "fail"}
    assert {"executable:claude", "executable:node", "dependency:missing-package"} <= failures


def test_malformed_configuration_is_reported_without_echoing_content(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "claude_native_bridge: [secret-looking-value", encoding="utf-8"
    )
    report = doctor(
        tmp_path,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name="linux",
    )

    config = next(c for c in report["checks"] if c["id"] == "configuration")
    assert config["status"] == "fail"
    assert "secret-looking-value" not in json.dumps(report)


def test_consent_is_reported_without_reading_auth_or_private_state(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": False})
    report = doctor(
        tmp_path,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name=sys.platform,
    )

    consent = next(c for c in report["checks"] if c["id"] == "development_channel_consent")
    assert consent["status"] == "fail"
    assert consent["accepted"] is False
    assert report["ready"] is False
    assert not (tmp_path / "claude-native-bridge").exists()


def test_dependency_versions_must_satisfy_the_full_declared_range(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})
    report = doctor(
        tmp_path,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=lambda name: "0.26.9",
        required_distributions=None,
        platform_name="linux",
    )

    httpx = next(c for c in report["checks"] if c["id"] == "dependency:httpx")
    assert httpx["status"] == "fail"
    assert report["ready"] is False


def test_python_version_must_satisfy_the_declared_range(tmp_path, monkeypatch):
    _write_config(tmp_path, {"development_channels_accepted": True})

    for version, expected_status in (
        ((3, 10, 9), "fail"),
        ((3, 11, 0), "pass"),
        ((3, 13, 9), "pass"),
        ((3, 14, 0), "pass"),
        ((3, 15, 0), "pass"),
    ):
        monkeypatch.setattr(
            diagnostics, "_current_python_version", lambda version=version: version
        )
        report = doctor(
            tmp_path,
            executable_resolver=lambda name: f"/safe/{name}",
            distribution_version=_versions,
            required_distributions=("PyYAML",),
            platform_name="linux",
        )
        python = next(c for c in report["checks"] if c["id"] == "python")
        assert python["status"] == expected_status
        assert python["detail"] == "Python >=3.11 is required."


def test_missing_channel_dependencies_are_reported(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})
    empty_channel = tmp_path / "channel"
    empty_channel.mkdir()
    report = doctor(
        tmp_path,
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
        platform_name="linux",
        channel_root=empty_channel,
    )

    failures = {c["id"] for c in report["checks"] if c["status"] == "fail"}
    assert "dependency:@modelcontextprotocol/sdk" in failures
    assert "dependency:zod" in failures


def test_platform_policy_reports_known_hosts_and_unknown_ones(tmp_path):
    _write_config(tmp_path, {"development_channels_accepted": True})
    common = dict(
        executable_resolver=lambda name: f"/safe/{name}",
        distribution_version=_versions,
        required_distributions=("PyYAML",),
    )
    linux = doctor(tmp_path, platform_name="linux", **common)
    unknown = doctor(tmp_path, platform_name="plan9", **common)

    linux_platform = next(c for c in linux["checks"] if c["id"] == "platform")
    unknown_platform = next(c for c in unknown["checks"] if c["id"] == "platform")
    assert linux_platform["compatibility"] == "known"
    assert linux_platform["status"] == "pass"
    assert unknown_platform["compatibility"] == "unknown"
    assert unknown_platform["status"] == "warn"
    assert unknown["ready"] is True


def test_cli_doctor_prints_json_without_importing_lifecycle_module(
    tmp_path, capsys, monkeypatch
):
    import builtins

    _write_config(tmp_path, {"development_channels_accepted": False})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "api_service" or name.endswith(".api_service"):
            raise AssertionError("offline doctor imported the lifecycle module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    code = main(["doctor", "--home", str(tmp_path)])

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["offline"] is True
