"""Import-only check run inside the server runtime; it starts nothing.

Every third-party module must resolve inside the runtime's own prefix, which
proves no host dependency path leaked into the isolated launch.
"""

import importlib
import json
from pathlib import Path
import platform
import sys

THIRD_PARTY = ["uvicorn", "fastapi", "yaml", "jsonschema", "httpx", "psutil", "filelock"]
BRIDGE = [
    "claude_native_bridge.api",
    "claude_native_bridge.api_server",
    "claude_native_bridge.client",
    "claude_native_bridge.native",
    "claude_native_bridge.protocol",
]
if sys.platform == "win32":
    THIRD_PARTY += ["winpty", "pyte", "win32api"]
    BRIDGE += [
        "claude_native_bridge.windows_terminal",
        "claude_native_bridge.windows_security",
    ]


def main():
    prefix = Path(sys.prefix).resolve()
    outside = []
    for name in THIRD_PARTY:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(prefix):
            outside.append(name)
    for name in BRIDGE:
        importlib.import_module(name)
    if outside:
        raise SystemExit("ImportError: modules resolved outside the runtime: " + ", ".join(outside))
    print(
        json.dumps(
            {
                "python": sys.executable,
                "version": platform.python_version(),
                "package": str(Path(sys.modules["claude_native_bridge"].__file__).resolve().parent),
                "modules": len(THIRD_PARTY) + len(BRIDGE),
            }
        )
    )


if __name__ == "__main__":
    main()
