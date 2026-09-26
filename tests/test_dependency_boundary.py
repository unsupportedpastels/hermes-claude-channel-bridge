import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Hermes's own interface is imported first: its dependencies are Hermes's
# business. After that, the plugin must register without the OpenAI SDK,
# pydantic or any server-only package, so a host launcher that cannot load
# them (e.g. a mismatched native ABI) still lists the provider.
CODE = r"""
import json, sys
import providers, providers.base
BLOCKED = {"openai", "pydantic", "pydantic_core", "fastapi", "uvicorn", "jsonschema",
           "winpty", "pyte"}
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked " + name)
for name in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
    del sys.modules[name]
sys.meta_path.insert(0, Block())
import claude_native_bridge.provider as provider
provider.register()
print(json.dumps({"name": provider.profile.name,
                  "module": provider.__file__,
                  "loaded": sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)}))
"""


def test_provider_registers_without_sdk_or_server_packages(tmp_path):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), PYTHONPATH=str(ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", CODE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["name"] == "claude-native-bridge"
    assert Path(report["module"]).resolve().is_relative_to(ROOT)
    assert report["loaded"] == []
