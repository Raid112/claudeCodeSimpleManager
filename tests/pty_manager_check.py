"""Checks for the process_exited producer in terminal/pty_manager.py (Task 4 of
the Hermes plan). Uses a fake winpty.PTY whose isalive() the test toggles
directly, so no real process is spawned. Redirects %LOCALAPPDATA% to a fresh
temp dir so work_items.log_event() never touches the real event log.

Mirrors the fake-PTY approach in tests/pty_env_check.py (Task 0), but that file
stays scoped to build_child_env/spawn-time identity; this one is scoped to the
alive->dead transition and its process_exited event.
"""

import base64
import os
import sys
import shutil
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}  {extra}")


class FakePTY:
    """Stand-in for winpty.PTY. isalive() is a plain attribute the test flips
    directly (no real process, no timing games needed to simulate an exit)."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.alive = True

    pid = None  # _kill_tree no-ops on a falsy pid

    def spawn(self, appname, cmdline=None, cwd=None, env=None):
        self.cmdline = cmdline
        return True

    def isalive(self):
        return self.alive

    def read(self):
        return ""

    def write(self, data):
        return True

    def set_size(self, cols, rows):
        pass


class _FakeWinptyModule:
    PTY = FakePTY


def _decode_ps(cmdline: str) -> str:
    return base64.b64decode(cmdline.split("-EncodedCommand ")[1]).decode("utf-16le")


def _exit_events(work_items, hermes_events, session_key):
    return [e for e in work_items.read_events()
            if e.get("session_key") == session_key
            and e.get("event") == hermes_events.PROCESS_EXITED_EVENT]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pty_manager_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)

    import terminal.pty_manager as pty_manager_module
    from terminal.pty_manager import PtyManager
    from terminal import work_items, hermes_events

    print(f"data dir -> {work_items.get_data_dir()}\n")

    original_winpty = pty_manager_module.winpty
    pty_manager_module.winpty = _FakeWinptyModule
    try:
        # 1. unexpected death emits exactly one process_exited, and a repeat
        # poll on the still-dead session does not duplicate it.
        manager = PtyManager(hooks_settings_path=None)
        s1 = manager.create_session("group", "C:/tmp", terminal_type="powershell",
                                    session_key="skey-unexpected-1")
        manager.get_all_sessions()  # alive: no event yet
        check("1 alive session: no process_exited", len(_exit_events(work_items, hermes_events, s1.session_key)) == 0)

        s1.pty.alive = False
        manager.get_all_sessions()
        check("1 unexpected death: exactly one process_exited",
              len(_exit_events(work_items, hermes_events, s1.session_key)) == 1)

        manager.get_all_sessions()  # still dead, polled again
        check("1 repeat poll on dead session: still exactly one",
              len(_exit_events(work_items, hermes_events, s1.session_key)) == 1)

        # 2. claude_session_id rides along, and normalize_event maps it correctly
        s2 = manager.create_session("group", "C:/tmp", terminal_type="claude",
                                    session_key="skey-csid-1", claude_session_id="csid-abc")
        s2.pty.alive = False
        manager.get_all_sessions()
        evs = _exit_events(work_items, hermes_events, s2.session_key)
        check("2 exactly one event", len(evs) == 1, str(evs))
        check("2 claude_session_id rides along", evs and evs[0].get("claude_session_id") == "csid-abc")
        if evs:
            normalized = hermes_events.normalize_event(evs[0], None)
            check("2 normalize_event -> process_exited/exited",
                  normalized is not None and normalized["event_type"] == "process_exited"
                  and normalized["status"] == "exited", str(normalized))

        # 3. close_session (intentional) emits no process_exited, even though
        # close_session pops the session from self.sessions before killing it.
        s3 = manager.create_session("group", "C:/tmp", terminal_type="powershell",
                                    session_key="skey-close-1")
        manager.close_session(s3.id)
        check("3 closed session removed from tracking", s3.id not in manager.sessions)
        sessions_after = manager.get_all_sessions()
        check("3 get_all_sessions no longer lists it",
              not any(s["session_key"] == "skey-close-1" for s in sessions_after))
        check("3 close_session emits no process_exited",
              len(_exit_events(work_items, hermes_events, "skey-close-1")) == 0)

        # 4. close_all (app shutdown) emits none across several sessions.
        keys = [f"skey-closeall-{i}" for i in range(3)]
        for key in keys:
            manager.create_session("group", "C:/tmp", terminal_type="powershell", session_key=key)
        manager.close_all()
        all_closeall_events = [e for e in work_items.read_events()
                               if e.get("session_key") in keys
                               and e.get("event") == hermes_events.PROCESS_EXITED_EVENT]
        check("4 close_all emits no process_exited for any session",
              len(all_closeall_events) == 0, str(all_closeall_events))
        check("4 close_all clears the sessions dict", len(manager.sessions) == 0)

        # 5. registry.is_closing() suppresses the narrow race: a session still
        # present in self.sessions (not yet popped) but already marked closing
        # (begin_close called, matching what close_session does before it pops
        # and kills) must not be reported even though it now looks dead.
        s5 = manager.create_session("group", "C:/tmp", terminal_type="powershell",
                                    session_key="skey-race-1")
        s5.pty.alive = False
        assert manager.registry.begin_close("skey-race-1")
        try:
            manager._maybe_report_exit(s5, False)
        finally:
            manager.registry.finish_close("skey-race-1")
        check("5 is_closing race window suppresses process_exited",
              len(_exit_events(work_items, hermes_events, "skey-race-1")) == 0)
        # once finish_close ran, a genuinely later unexpected death is NOT
        # suppressed forever (is_closing is False again, and this session_key
        # was never added to _exit_reported by the suppressed call above).
        manager._maybe_report_exit(s5, False)
        check("5 after finish_close, the same key can still report a real exit",
              len(_exit_events(work_items, hermes_events, "skey-race-1")) == 1)

        # 6. concurrent get_all_sessions() callers on one dead session emit
        # exactly one process_exited, never one-per-thread.
        s6 = manager.create_session("group", "C:/tmp", terminal_type="powershell",
                                    session_key="skey-concurrent-1")
        s6.pty.alive = False
        errors = []

        def poll():
            try:
                manager.get_all_sessions()
            except Exception as exc:  # pragma: no cover - surfaced via check() below
                errors.append(exc)

        threads = [threading.Thread(target=poll) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        check("6 no exceptions from concurrent get_all_sessions", not errors, str(errors))
        check("6 exactly one process_exited despite 20 concurrent pollers",
              len(_exit_events(work_items, hermes_events, "skey-concurrent-1")) == 1)

        # 7. restart_session respawns in place: same tab id + session_key, new PTY,
        # resumes the CURRENT claude session (the one /clear or /resume swapped to),
        # kills the old process tree first, and the ws reader never sees the session
        # as dead mid-swap (so no process_exited and no websocket close).
        killed = []
        original_kill = pty_manager_module._kill_tree
        pty_manager_module._kill_tree = lambda pty: (killed.append(pty), setattr(pty, "alive", False))
        try:
            s7 = manager.create_session("group", "C:/tmp", terminal_type="claude",
                                        session_key="skey-restart-1", claude_session_id="csid-old")
            s7.claude_session_id = "csid-swapped"
            old_pty = s7.pty
            seen_alive = []
            real_spawn = s7._spawn

            def spying_spawn(*a, **kw):
                seen_alive.append(s7.is_alive)  # old pty already killed at this point
                return real_spawn(*a, **kw)
            s7._spawn = spying_spawn
            ok = manager.restart_session(s7.id, 100, 40)
            check("7 restart returns True", ok is True)
            check("7 same tab id still tracked", manager.get_session(s7.id) is s7)
            check("7 old pty killed", killed == [old_pty])
            check("7 new pty in place, alive", s7.pty is not old_pty and s7.is_alive)
            check("7 is_alive stays True mid-swap", seen_alive == [True], str(seen_alive))
            check("7 new pty sized from the call", (s7.pty.cols, s7.pty.rows) == (100, 40))
            script = _decode_ps(s7.pty.cmdline)
            check("7 resumes the current session id", "claude --resume csid-swapped" in script, script)
            manager.get_all_sessions()
            check("7 no process_exited from a restart",
                  len(_exit_events(work_items, hermes_events, "skey-restart-1")) == 0)
            check("7 unknown tab -> False", manager.restart_session("nope", 80, 24) is False)

            # claude tab with no known session id falls back to --continue, not a fresh chat
            s7b = manager.create_session("group", "C:/tmp", terminal_type="claude",
                                         session_key="skey-restart-2", continue_session=True)
            manager.restart_session(s7b.id, 80, 24)
            script = _decode_ps(s7b.pty.cmdline)
            check("7 no session id -> claude --continue", "claude --continue" in script, script)
        finally:
            pty_manager_module._kill_tree = original_kill

        print(f"\n{_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        pty_manager_module.winpty = original_winpty
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
