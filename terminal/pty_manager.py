"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.
"""

import uuid
import threading
import time
import winpty
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _path_to_project_dir(path: str) -> str:
    """Convert a filesystem path to Claude's project directory name."""
    normalized = path.replace("\\", "/")
    return normalized.replace(":", "-").replace("/", "-")


def _detect_claude_session_id(session, pre_existing: set):
    """Background task: poll for new .jsonl file in Claude project dir."""
    project_dir = CLAUDE_PROJECTS_DIR / _path_to_project_dir(session.path)
    for _ in range(30):  # 30 * 0.5s = 15s max
        time.sleep(0.5)
        if not project_dir.exists():
            continue
        current = {f.name for f in project_dir.glob("*.jsonl")}
        new_files = current - pre_existing
        if new_files:
            newest = sorted(new_files, key=lambda f: (project_dir / f).stat().st_mtime)[-1]
            session.claude_session_id = newest.replace(".jsonl", "")
            return


class PtySession:
    """A single PTY session running claude CLI."""

    def __init__(self, session_id: str, group_name: str, path: str, cols: int, rows: int,
                 continue_session: bool = False, claude_session_id: str = None):
        self.id = session_id
        self.group_name = group_name
        self.path = path
        self.claude_session_id = claude_session_id
        self.pty = winpty.PTY(cols, rows)
        if claude_session_id:
            cmdline = f"claude --resume {claude_session_id}"
        elif continue_session:
            cmdline = "claude --continue"
        else:
            cmdline = "claude"
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

    def create_session(self, group_name: str, path: str, cols: int = 120, rows: int = 30,
                       continue_session: bool = False, claude_session_id: str = None) -> PtySession:
        # Snapshot existing session files before spawning
        project_dir = CLAUDE_PROJECTS_DIR / _path_to_project_dir(path)
        pre_existing = set()
        if project_dir.exists():
            pre_existing = {f.name for f in project_dir.glob("*.jsonl")}

        session_id = str(uuid.uuid4())[:8]
        session = PtySession(session_id, group_name, path, cols, rows,
                             continue_session=continue_session, claude_session_id=claude_session_id)
        self.sessions[session_id] = session

        # Detect Claude session ID asynchronously if not already known
        if not claude_session_id:
            t = threading.Thread(target=_detect_claude_session_id,
                                 args=(session, pre_existing), daemon=True)
            t.start()

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
                "claude_session_id": s.claude_session_id,
            })
        return result

    def close_all(self):
        for session in list(self.sessions.values()):
            session.kill()
        self.sessions.clear()
