import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from claude_native_bridge import runtime_environment as rt

ROOT = Path(__file__).resolve().parents[1]


def _project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_server_extra_is_the_runtime_requirement_list():
    assert tuple(_project()["optional-dependencies"]["server"]) == rt.SERVER_REQUIREMENTS


def test_no_python_requirement_and_only_minimum_versions():
    project = _project()
    assert "requires-python" not in project
    for requirement in [*project["dependencies"], *rt.SERVER_REQUIREMENTS]:
        specifier = requirement.split(";")[0]
        assert ">=" in specifier, requirement
        assert not any(op in specifier for op in ("<", "==", "~=", "!=")), requirement


def test_server_only_packages_stay_out_of_hermes_dependencies():
    host = {r.split(">=")[0].strip().lower() for r in _project()["dependencies"]}
    assert not host & {"fastapi", "uvicorn", "jsonschema", "pywinpty", "pyte"}


def test_launch_imports_only_the_given_package_and_ignores_host_paths(
    tmp_path, monkeypatch
):
    package = tmp_path / "checkout" / "claude_native_bridge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "report.py").write_text(
        "import json, sys\n"
        "import claude_native_bridge as pkg\n"
        "print(json.dumps({'path': sys.path, 'argv': sys.argv[1:],"
        " 'isolated': sys.flags.isolated, 'utf8': sys.flags.utf8_mode,"
        " 'package': pkg.__file__}))\n"
    )
    poison = tmp_path / "poison"
    (poison / "claude_native_bridge").mkdir(parents=True)
    (poison / "claude_native_bridge" / "__init__.py").write_text(
        "raise ImportError('host path leaked')"
    )
    monkeypatch.setattr(rt, "package_directory", lambda: package)
    env = dict(os.environ, PYTHONPATH=str(poison), PYTHONHOME=str(tmp_path / "none"))

    completed = subprocess.run(
        rt.command(sys.executable, "claude_native_bridge.report", "--port", 7),
        env=env,
        cwd=poison,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    report = json.loads(completed.stdout)
    assert report["isolated"] == 1 and report["utf8"] == 1
    assert report["argv"] == ["--port", "7"]
    assert Path(report["package"]).resolve() == (package / "__init__.py").resolve()
    assert not any(str(poison) in entry for entry in report["path"])


def test_bootstrap_survives_windows_command_line_quoting():
    assert '"' not in rt._BOOTSTRAP and "\n" not in rt._BOOTSTRAP


def test_child_environment_drops_ambient_python_paths(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/leak")
    monkeypatch.setenv("VIRTUAL_ENV", "/old/venv")
    env = rt.child_environment(HERMES_HOME="/h")
    assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env
    assert env["HERMES_HOME"] == "/h"


def test_error_summary_keeps_only_the_exception_line():
    text = "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'yaml'\n"
    assert rt.error_summary(text) == "ModuleNotFoundError: No module named 'yaml'"
    assert rt.error_summary("no failure here") is None


def _marker(home, **record):
    root = rt.environment_root(home)
    root.mkdir(parents=True)
    base = {
        "backend": "venv",
        "python": sys.executable,
        "requirements": rt.requirements_digest(),
        "validated_python": sys.executable,
    }
    (root / rt.MARKER).write_text(json.dumps({**base, **record}))


@pytest.mark.parametrize(
    "record, state",
    [
        ({}, "ready"),
        ({"requirements": "older"}, "outdated"),
        ({"validated_python": "/elsewhere/python"}, "unvalidated"),
        ({"python": "/gone/python"}, "broken"),
    ],
)
def test_status_reads_selection_without_running_anything(tmp_path, record, state):
    _marker(tmp_path, **record)
    assert rt.status(tmp_path)["state"] == state


def test_missing_runtime_is_reported_with_the_repair_command(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "provision", lambda home: pytest.fail("must not provision"))
    assert rt.status(tmp_path)["state"] == "missing"
    with pytest.raises(rt.RuntimeUnavailable, match="hermes-claude-bridge repair"):
        rt.ensure_ready(tmp_path)


def test_provisioning_happens_only_when_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "provision", lambda home: Path("/prepared/python"))
    assert rt.ensure_ready(tmp_path, allow_provision=True) == Path("/prepared/python")
