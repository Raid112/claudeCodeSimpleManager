"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.
"""

import re
import uuid
import threading
import time
import winpty
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Lock per path to serialize detection for same project directory
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _get_path_lock(path: str) -> threading.Lock:
    with _path_locks_guard:
        if path not in _path_locks:
            _path_locks[path] = threading.Lock()
        return _path_locks[path]


def _path_to_project_dir(path: str) -> str:
    """Convert a filesystem path to Claude's project directory name."""
    normalized = path.replace("\\", "/")
    return normalized.replace(":", "-").replace("/", "-")


def _get_session_entries(project_dir: Path) -> set:
    """Get all session UUIDs present as .jsonl files or directories."""
    entries = set()
    if not project_dir.exists():
        return entries
    for item in project_dir.iterdir():
        name = item.stem if item.suffix == ".jsonl" else item.name
        if UUID_PATTERN.match(name):
            entries.add(name)
    return entries


def _detect_claude_session_id(session, pre_existing: set, path_lock: threading.Lock):
    """Background task: poll for new session entry in Claude project dir."""
    with path_lock:
        project_dir = CLAUDE_PROJECTS_DIR / _path_to_project_dir(session.path)
        for _ in range(60):  # 60 * 0.5s = 30s max
            time.sleep(0.5)
            current = _get_session_entries(project_dir)
            new_entries = current - pre_existing
            if new_entries:
                # Pick the newest by checking .jsonl mtime, or dir mtime
                def entry_mtime(name):
                    jsonl = project_dir / f"{name}.jsonl"
                    if jsonl.exists():
                        return jsonl.stat().st_mtime
                    d = project_dir / name
                    if d.exists():
                        return d.stat().st_mtime
                    return 0

                newest = max(new_entries, key=entry_mtime)
                session.claude_session_id = newest
                return


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
    ):
        self.id = session_id
        self.group_name = group_name
        self.path = path
        self.terminal_type = terminal_type
        self.claude_session_id = claude_session_id
        self.pty = winpty.PTY(cols, rows)
        if terminal_type == "claude":
            if claude_session_id:
                cmdline = f"claude --resume {claude_session_id}"
            elif continue_session:
                cmdline = "claude --continue"
            else:
                cmdline = "claude"
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
        self._detect_threads: list[threading.Thread] = []

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
        session = PtySession(
            session_id,
            group_name,
            path,
            cols,
            rows,
            terminal_type=terminal_type,
            continue_session=continue_session,
            claude_session_id=claude_session_id,
        )
        self.sessions[session_id] = session

        # Detect Claude session ID asynchronously only for claude type
        if terminal_type == "claude" and not claude_session_id:
            project_dir = CLAUDE_PROJECTS_DIR / _path_to_project_dir(path)
            pre_existing = _get_session_entries(project_dir)
            path_lock = _get_path_lock(path)
            t = threading.Thread(
                target=_detect_claude_session_id,
                args=(session, pre_existing, path_lock),
                daemon=True,
            )
            t.start()
            self._detect_threads.append(t)

        return session

    def wait_for_detection(self, timeout: float = 5.0):
        """Wait for all detection threads to finish (up to timeout)."""
        for t in self._detect_threads:
            t.join(timeout=timeout)
        self._detect_threads = [t for t in self._detect_threads if t.is_alive()]

    def get_session(self, session_id: str) -> PtySession | None:
        return self.sessions.get(session_id)

    def close_session(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            session.kill()

    def get_all_sessions(self) -> list[dict]:
        result = []
        for s in self.sessions.values():
            result.append(
                {
                    "id": s.id,
                    "group_name": s.group_name,
                    "path": s.path,
                    "is_alive": s.is_alive,
                    "claude_session_id": s.claude_session_id,
                    "terminal_type": s.terminal_type,
                }
            )
        return result

    def close_all(self):
        for session in list(self.sessions.values()):
            session.kill()
        self.sessions.clear()
