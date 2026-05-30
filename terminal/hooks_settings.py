"""
Generates, at startup, the per-session `--settings` JSON that wires Claude Code
hooks back into this app.

Why generated at runtime (not a static asset): the command must point at THIS
running executable, whose path is only known now. When frozen (PyInstaller),
`sys.executable` is the GUI exe itself, so the hook re-invokes it with
`--hook-notify` (handled at the top of main.py before heavy imports). In dev,
`sys.executable` is python, so we also pass the main.py path.

Paths use forward slashes and are double-quoted: a backslash in the command
string makes the hook process fail, and a failing UserPromptSubmit hook BLOCKS
the user's turn (validated empirically). Forward slashes + quotes avoid both
the backslash hazard and spaces in the path.
"""

import sys
import json
import os
from pathlib import Path

from terminal.hook_state import get_data_dir

# All hook events route to the same command; the branch in main.py reads
# `hook_event_name` from the stdin payload to know which one fired.
_HOOK_EVENTS_WITH_MATCHER = ("PreToolUse", "PostToolUse")
_HOOK_EVENTS_PLAIN = ("UserPromptSubmit", "Stop", "Notification")


def _hook_command() -> str:
    exe = sys.executable.replace("\\", "/")
    parts = [f'"{exe}"']
    if not getattr(sys, "frozen", False):
        # Dev: sys.executable is python -> need the script path too.
        main_py = (Path(__file__).resolve().parent.parent / "main.py").as_posix()
        parts.append(f'"{main_py}"')
    parts.append("--hook-notify")
    return " ".join(parts)


def generate_hooks_settings() -> str:
    """Write the hooks settings JSON and return its absolute path (forward-slashed)."""
    command = _hook_command()
    hooks: dict = {}
    for event in _HOOK_EVENTS_PLAIN:
        hooks[event] = [{"hooks": [{"type": "command", "command": command}]}]
    for event in _HOOK_EVENTS_WITH_MATCHER:
        hooks[event] = [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]

    path = get_data_dir() / "hooks_settings.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"hooks": hooks}, f, indent=2)
    os.replace(tmp, path)
    return path.as_posix()
