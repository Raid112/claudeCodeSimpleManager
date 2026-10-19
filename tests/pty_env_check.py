"""Checks for stable tab/session identity at spawn (Task 0 of the Hermes plan).

Covers `build_child_env` (pure, no process spawned) and a concurrency check that
two `PtyManager.create_session` calls racing on two threads never cross-assign
their `CLAUDEMANAGER_TAB` / `CLAUDEMANAGER_SESSION_KEY` env pairs — the bug this
task fixes (see terminal/pty_manager.py, previously mutated os.environ on the
parent right before spawn). Uses a fake winpty.PTY; no real process is spawned.
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terminal.pty_manager as pty_manager_module
from terminal.pty_manager import PtyManager, build_child_env


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  ok   {name}")


class FakePTY:
    """Stand-in for winpty.PTY: records spawn() calls, never touches a real process."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.spawn_calls = []

    def spawn(self, appname, cmdline=None, cwd=None, env=None):
        # Sleep so both threads' create_session calls are genuinely in flight at
        # the same time — widens the window a reintroduced shared-state bug (e.g.
        # mutating os.environ instead of passing env=...) would need to hit.
        time.sleep(0.05)
        self.spawn_calls.append({"appname": appname, "cmdline": cmdline, "cwd": cwd, "env": env})
        return True

    def isalive(self):
        return True

    def read(self):
        return ""

    def write(self, data):
        return True

    def set_size(self, cols, rows):
        pass


class _FakeWinptyModule:
    PTY = FakePTY


def check_pure_build_child_env() -> None:
    # "=C:" mimics a Windows per-drive cwd pseudo-variable: a valid os.environ key
    # on Windows, but not a valid name=value pair for CreateProcessW's env block.
    base = {"PATH": r"C:\Windows;C:\Windows\System32", "SystemRoot": r"C:\Windows", "=C:": r"C:\repo"}
    block = build_child_env(base, "tab-123", "session-key-abc")

    check("contains CLAUDEMANAGER_TAB", "CLAUDEMANAGER_TAB=tab-123" in block)
    check("contains CLAUDEMANAGER_SESSION_KEY", "CLAUDEMANAGER_SESSION_KEY=session-key-abc" in block)
    check("preserves seeded PATH", "PATH=C:\\Windows;C:\\Windows\\System32" in block)
    check("is null-separated", "\0" in block)
    check("is null-terminated", block.endswith("\0"))

    entries = [e for e in block.split("\0") if e]
    check("drops '='-prefixed pseudo keys", all(not e.startswith("=") for e in entries))
    check("kept every real entry (PATH, SystemRoot, + 2 injected)", len(entries) == 4)


def check_concurrent_create_session() -> None:
    # This shell itself runs inside a real ClaudeManager-managed tab, so
    # CLAUDEMANAGER_TAB/CLAUDEMANAGER_SESSION_KEY may already be set on the real
    # process env — save and restore rather than assuming they start unset.
    saved_tab = os.environ.get("CLAUDEMANAGER_TAB")
    saved_key = os.environ.get("CLAUDEMANAGER_SESSION_KEY")
    original_winpty = pty_manager_module.winpty
    pty_manager_module.winpty = _FakeWinptyModule
    try:
        manager = PtyManager(hooks_settings_path=None)
        results = {}
        errors = []

        def spawn_one(name, key):
            try:
                # terminal_type="powershell" => build_resume_command returns "" and
                # no codex/opencode discovery thread starts; only the fake PTY runs.
                results[name] = manager.create_session(
                    "group", "C:/tmp", terminal_type="powershell", session_key=key)
            except Exception as exc:  # pragma: no cover - surfaced via check() below
                errors.append(exc)

        t1 = threading.Thread(target=spawn_one, args=("a", "session-key-alpha"))
        t2 = threading.Thread(target=spawn_one, args=("b", "session-key-beta"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        check("no exceptions during concurrent create_session", not errors)
        check("two distinct sessions were created", len(results) == 2)
        check("sessions got distinct manager (tab) ids", results["a"].id != results["b"].id)
        check(
            "sessions kept their own session_key",
            results["a"].session_key == "session-key-alpha" and results["b"].session_key == "session-key-beta",
        )

        for name, session in results.items():
            other = results["b"] if name == "a" else results["a"]
            fake_pty = session.pty
            check(f"session {name} recorded exactly one spawn", len(fake_pty.spawn_calls) == 1)
            env = fake_pty.spawn_calls[0]["env"]
            check(
                f"session {name} env pairs its OWN tab id with its OWN session_key",
                f"CLAUDEMANAGER_TAB={session.id}" in env and f"CLAUDEMANAGER_SESSION_KEY={session.session_key}" in env,
            )
            check(
                f"session {name} env does not leak the other session's tab id",
                f"CLAUDEMANAGER_TAB={other.id}" not in env,
            )
            check(
                f"session {name} env does not leak the other session's session_key",
                f"CLAUDEMANAGER_SESSION_KEY={other.session_key}" not in env,
            )

        check(
            "os.environ CLAUDEMANAGER_TAB unaffected by concurrent create_session",
            os.environ.get("CLAUDEMANAGER_TAB") == saved_tab,
        )
        check(
            "os.environ CLAUDEMANAGER_SESSION_KEY unaffected by concurrent create_session",
            os.environ.get("CLAUDEMANAGER_SESSION_KEY") == saved_key,
        )
    finally:
        pty_manager_module.winpty = original_winpty
        for var, saved in (("CLAUDEMANAGER_TAB", saved_tab), ("CLAUDEMANAGER_SESSION_KEY", saved_key)):
            if saved is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = saved


def main() -> int:
    check_pure_build_child_env()
    check_concurrent_create_session()
    print("\npty env checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
