"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.

Each claude session is spawned with a session id we own (`--session-id <uuid>`)
and per-session hooks (`--settings <generated file>`). Those hooks report the
session's authoritative state (running/ready/tooluse/waiting) via terminal/hook_state.py,
which `get_all_sessions` reads back. There is no longer any need to poll the
~/.claude/projects directory to discover the session id.
"""

import uuid
import winpty

from terminal.input_debug import log_input_boundary
from terminal.hook_state import delete_state, read_state


class PtySession:
    """A single PTY session running a terminal command."""

    def __init__(
        self,
        session_id: str,
        group_name: str,
        path: str,
        cols: int,
        rows: int,
        terminal_type: str = "claude",
        continue_session: bool = False,
        claude_session_id: str = None,
        resume: bool = False,
        hooks_settings_path: str = None,
    ):
        self.id = session_id
        self.group_name = group_name
        self.path = path
        self.terminal_type = terminal_type
        self.claude_session_id = claude_session_id
        self.pty = winpty.PTY(cols, rows)

        if terminal_type == "claude":
            settings_arg = f' --settings "{hooks_settings_path}"' if hooks_settings_path else ""
            if resume and claude_session_id:
                base = f"claude --resume {claude_session_id}"
            elif claude_session_id:
                base = f"claude --session-id {claude_session_id}"
            elif continue_session:
                base = "claude --continue"
            else:
                base = "claude"
            cmdline = base + settings_arg
        elif terminal_type == "opencode":
            cmdline = "opencode"
        elif terminal_type == "codex":
            cmdline = "codex"
        else:
            cmdline = ""

        self.pty.spawn(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmdline=cmdline,
            cwd=path,
        )

    @property
    def is_alive(self) -> bool:
        try:
            return self.pty.isalive()
        except Exception:
            return False

    def read(self) -> str:
        try:
            return self.pty.read()
        except Exception:
            return ""

    def write(self, data: str):
        try:
            log_input_boundary("pty-write", data, session_id=self.id)
            self.pty.write(data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int):
        try:
            self.pty.set_size(cols, rows)
        except Exception:
            pass

    def kill(self):
        try:
            self.pty.write("exit\r\n")
        except Exception:
            pass


class PtyManager:
    """Manages all PTY sessions."""

    def __init__(self, hooks_settings_path: str = None):
        self.sessions: dict[str, PtySession] = {}
        self.hooks_settings_path = hooks_settings_path

    def create_session(
        self,
        group_name: str,
        path: str,
        cols: int = 120,
        rows: int = 30,
        terminal_type: str = "claude",
        continue_session: bool = False,
        claude_session_id: str = None,
    ) -> PtySession:
        session_id = str(uuid.uuid4())[:8]

        # Decide how to launch claude and what session id it will own.
        resume = False
        if terminal_type == "claude":
            if claude_session_id:
                resume = True  # restoring a known session
                # Discard any stale state from a previous run: the re-spawned
                # session starts idle, so a leftover 'running' would stick until
                # the next turn. Heuristic covers the gap until the first hook.
                delete_state(claude_session_id)
            elif not continue_session:
                claude_session_id = str(uuid.uuid4())  # fresh session, we own the id

        session = PtySession(
            session_id,
            group_name,
            path,
            cols,
            rows,
            terminal_type=terminal_type,
            continue_session=continue_session,
            claude_session_id=claude_session_id,
            resume=resume,
            hooks_settings_path=self.hooks_settings_path,
        )
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> PtySession | None:
        return self.sessions.get(session_id)

    def close_session(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            if session.claude_session_id:
                delete_state(session.claude_session_id)
            session.kill()

    def get_all_sessions(self) -> list[dict]:
        result = []
        for s in self.sessions.values():
            st = read_state(s.claude_session_id) if s.claude_session_id else None
            result.append(
                {
                    "id": s.id,
                    "group_name": s.group_name,
                    "path": s.path,
                    "is_alive": s.is_alive,
                    "claude_session_id": s.claude_session_id,
                    "terminal_type": s.terminal_type,
                    "state": st.get("status") if st else None,
                    "state_ts": st.get("ts") if st else None,
                }
            )
        return result

    def close_all(self):
        for session in list(self.sessions.values()):
            if session.claude_session_id:
                delete_state(session.claude_session_id)
            session.kill()
        self.sessions.clear()
