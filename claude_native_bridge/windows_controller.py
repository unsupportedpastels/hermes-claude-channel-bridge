"""Adapt the existing startup-control contract to native Windows ConPTY."""

import json
from pathlib import Path
import subprocess


class WindowsController:
    def __init__(self, runtime, *, factory=None):
        self.runtime = Path(runtime)
        self.factory = factory
        self.terminal = None

    def command(self, *args, check=True):
        if not args:
            raise ValueError("Missing terminal action")
        action = args[0]
        stdout = ""
        returncode = 0
        if action == "new-session":
            if self.terminal is not None:
                raise RuntimeError("Terminal already started")
            from .windows_terminal import WindowsTerminal

            spec = json.loads((self.runtime / "launch.json").read_text())
            factory = self.factory or WindowsTerminal
            self.terminal = factory(
                argv=spec["argv"],
                cwd=self.runtime,
                env=spec["environment"],
                runtime=self.runtime,
                owner_pid=spec["owner_pid"],
            )
            self.terminal.start()
        elif action == "has-session":
            returncode = (
                0 if self.terminal is not None and self.terminal.is_alive() else 1
            )
        elif action == "capture-pane":
            if self.terminal is None:
                raise RuntimeError("Terminal not started")
            stdout = self.terminal.capture()
        elif action == "send-keys":
            if self.terminal is None:
                raise RuntimeError("Terminal not started")
            # NativeSession supplies -t worker, then one supported consent key.
            if len(args) != 4 or args[1:3] != ("-t", "worker"):
                raise ValueError("Unexpected input-control arguments")
            self.terminal.send_key(args[3])
        elif action == "kill-server":
            if self.terminal is not None:
                self.terminal.close()
        else:
            raise ValueError("Unsupported terminal-control action")
        result = subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, args, output=stdout)
        return result
