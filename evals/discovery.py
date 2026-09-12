"""Create an isolated test home and inspect the real provider resolution path."""

import os
from pathlib import Path
import tempfile
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="hermes-native-integration-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["TERMINAL_CWD"] = str(HOME)
(HOME / "plugins" / "model-providers").mkdir(parents=True)
(HOME / "plugins" / "model-providers" / "claude-native-bridge").symlink_to(
    ROOT, target_is_directory=True
)
(HOME / "config.yaml").write_text(
    yaml.safe_dump(
        {
            "model": {"provider": "claude-native-bridge", "default": "claude-sonnet-5"},
            "claude_native_bridge": {
                "development_channels_accepted": True,
                "effort": "low",
                "startup_timeout": 35,
                "retain_diagnostics": True,
            },
            "auxiliary": {"background_review": {"enabled": False}},
            "compression": {"enabled": False},
            "memory": {"memory_enabled": True, "user_profile_enabled": True},
            "plugins": {"enabled": ["claude-native-bridge"]},
        }
    )
)
from providers import get_provider_profile
from hermes_cli.auth import resolve_external_process_provider_credentials
from run_agent import AIAgent

p = get_provider_profile("claude-native-bridge")
print("profile:", p.name, p.auth_type, p.fetch_models())
creds = resolve_external_process_provider_credentials("claude-native-bridge")
print(
    "credential route:",
    {k: creds.get(k) for k in ["provider", "base_url", "command", "args", "source"]},
)
a = AIAgent(
    provider="claude-native-bridge",
    model="claude-sonnet-5",
    api_mode="chat_completions",
    session_id="bridge-discovery-" + str(uuid.uuid4()),
    enabled_toolsets=["terminal", "skills", "memory"],
    skip_context_files=True,
    skip_memory=False,
    skip_background_review=True,
    max_iterations=7,
    quiet_mode=True,
)
try:
    print("client:", type(a.client).__module__, type(a.client).__name__)
    print("tools:", [x.get("function", {}).get("name") for x in a.tools])
finally:
    a.close()
(ROOT / ".private").mkdir(exist_ok=True)
(ROOT / ".private" / "integration-home.txt").write_text(str(HOME))
print("isolated home:", HOME)
