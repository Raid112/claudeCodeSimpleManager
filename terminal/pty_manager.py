"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.
"""

import uuid
import winpty


class PtySession:
    """A single PTY session running claude CLI."""

    def __init__(self, session_id: str, group_name: str, path: str, cols: int, rows: int):
        self.id = session_id
        self.group_name = group_name
        self.path = path
        self.pty = winpty.PTY(cols, rows)
        self.pty.spawn(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmdline="claude",
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

    def __init__(self):
        self.sessions: dict[str, PtySession] = {}

    def create_session(self, group_name: str, path: str, cols: int = 120, rows: int = 30) -> PtySession:
        session_id = str(uuid.uuid4())[:8]
        session = PtySession(session_id, group_name, path, cols, rows)
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> PtySession | None:
        return self.sessions.get(session_id)

    def close_session(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            session.kill()

    def get_all_sessions(self) -> list[dict]:
        result = []
        for s in self.sessions.values():
            result.append({
                "id": s.id,
                "group_name": s.group_name,
                "path": s.path,
                "is_alive": s.is_alive,
            })
        return result

    def close_all(self):
        for session in list(self.sessions.values()):
            session.kill()
        self.sessions.clear()
