"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.

Each claude session is spawned with a session id we own (`--session-id <uuid>`)
and per-session hooks (`--settings <generated file>`). Those hooks report the
session's authoritative state (running/ready/tooluse/waiting) via terminal/hook_state.py,
which `get_all_sessions` reads back. There is no longer any need to poll the
~/.claude/projects directory to discover the session id.
"""

import os
import threading
import time
import uuid
import winpty
from pathlib import Path

from terminal.input_debug import log_input_boundary
from terminal.hook_state import delete_state, read_state
from terminal.agent_contracts import (
    ACTION_SEND_PROMPT, EXECUTION_SENT, EXECUTION_UNKNOWN, STATUS_APPROVED, STATUS_EXPIRED,
)
from terminal import agent_decisions
from terminal.input_protocol import prepare_composer_message, InputProtocolError
from terminal.session_registry import SessionRegistry
from terminal.session_support import (
    build_resume_command,
    find_codex_session_id,
    find_opencode_session_id,
    list_opencode_session_ids,
)


def _codex_sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def _opencode_database_path() -> Path:
    data_root = os.environ.get("XDG_DATA_HOME")
    if data_root:
        return Path(data_root) / "opencode" / "opencode.db"
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


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
        agent_session_id: str = None,
        session_key: str = None,
        resume: bool = False,
        hooks_settings_path: str = None,
    ):
        self.id = session_id
        self.group_name = group_name
        self.path = path
        self.terminal_type = terminal_type
        self.claude_session_id = claude_session_id
        self.agent_session_id = agent_session_id or claude_session_id
        self.session_key = session_key or str(uuid.uuid4())
        self.pty = winpty.PTY(cols, rows)

        cmdline = build_resume_command(
            terminal_type,
            session_id=(claude_session_id if terminal_type == "claude" else agent_session_id),
            continue_session=continue_session,
            hooks_path=hooks_settings_path,
            resume_session=resume,
        )

        # Tag the child env with this tab's id so the Claude Code hooks (grandchildren
        # of powershell) can report WHICH tab they belong to, independent of the
        # session_id they carry — which the user can swap via /clear or /resume.
        # Set on the parent right before spawn (env=None => child snapshots parent env);
        # create_session is synchronous, so no interleaving with another spawn.
        os.environ["CLAUDEMANAGER_TAB"] = self.id
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
            return True
        except Exception:
            return False

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
        self.registry = SessionRegistry()

    def create_session(
        self,
        group_name: str,
        path: str,
        cols: int = 120,
        rows: int = 30,
        terminal_type: str = "claude",
        continue_session: bool = False,
        claude_session_id: str = None,
        agent_session_id: str = None,
        session_key: str = None,
    ) -> PtySession:
        session_id = str(uuid.uuid4())[:8]

        # Decide how to launch claude and what session id it will own.
        resume = False
        if terminal_type == "claude":
            if claude_session_id:
                resume = True  # restoring a known session
                # No stale-state cleanup needed: state is keyed by tab id (a fresh
                # uuid per spawn), so the new tab starts with no state file — idle
                # until the first hook fires.
            elif not continue_session:
                claude_session_id = str(uuid.uuid4())  # fresh session, we own the id

        codex_root = _codex_sessions_root()
        codex_before = set(codex_root.rglob("*.jsonl")) if terminal_type == "codex" and codex_root.exists() else set()
        opencode_db = _opencode_database_path()
        opencode_before = list_opencode_session_ids(opencode_db) if terminal_type == "opencode" else set()
        started_at_ms = int(time.time() * 1000)

        session = PtySession(
            session_id,
            group_name,
            path,
            cols,
            rows,
            terminal_type=terminal_type,
            continue_session=continue_session,
            claude_session_id=claude_session_id,
            agent_session_id=agent_session_id,
            session_key=session_key,
            resume=resume,
            hooks_settings_path=self.hooks_settings_path,
        )
        self.sessions[session_id] = session
        self.registry.register(session)
        if terminal_type in {"codex", "opencode"} and not agent_session_id:
            thread = threading.Thread(
                target=self._discover_agent_session,
                args=(session, codex_root, codex_before, opencode_db, opencode_before, started_at_ms),
                daemon=True,
            )
            thread.start()
        return session

    @staticmethod
    def _discover_agent_session(
        session: PtySession,
        codex_root: Path,
        codex_before: set[Path],
        opencode_db: Path,
        opencode_before: set[str],
        started_at_ms: int,
    ):
        for _ in range(60):
            if session.agent_session_id:
                return
            if session.terminal_type == "codex":
                found = find_codex_session_id(codex_root, codex_before)
            else:
                found = find_opencode_session_id(opencode_db, session.path, started_at_ms, opencode_before)
            if found:
                session.agent_session_id = found
                try:
                    self.registry.refresh_aliases(session)
                except ValueError:
                    pass
                return
            if not session.is_alive:
                return
            time.sleep(0.5)

    def get_session(self, session_id: str) -> PtySession | None:
        return self.sessions.get(session_id)

    def close_session(self, session_id: str):
        session = self.sessions.get(session_id)
        if not session or not self.registry.begin_close(session.session_key):
            return
        try:
            self.sessions.pop(session_id, None)
            delete_state(session.id)  # state is keyed by tab id
            session.kill()
        finally:
            self.registry.finish_close(session.session_key)

    def find_by_session_key(self, session_key: str) -> PtySession | None:
        return self.registry.find_by_session_key(session_key)

    def send_prompt(self, session_key: str, text: str, request_id: str,
                    decision_id: str, proposal_hash: str) -> dict:
        """Write one approved semantic prompt and return a host-only receipt.

        A successful receipt means the host accepted the PTY write. It does not claim
        that a provider consumed or completed the prompt.
        """
        session = self.find_by_session_key(session_key)
        if session is None:
            raise KeyError(session_key)
        if session.terminal_type == "powershell":
            raise PermissionError("PowerShell is not a conversational provider")
        try:
            prepared = prepare_composer_message(
                text, bracketed_paste_mode=False, provider=session.terminal_type)
        except InputProtocolError:
            raise
        with self.registry.lock_for(session_key):
            decision = agent_decisions.get_decision(decision_id)
            if decision is None:
                raise PermissionError("prompt requires an approved proposal")
            if decision["proposal_hash"] != proposal_hash:
                raise ValueError("proposal hash does not match")
            if decision["status"] == STATUS_EXPIRED:
                raise ValueError("proposal_expired")
            action = decision["proposal"].get("action") or {}
            target = action.get("target") or {}
            parameters = action.get("parameters") or {}
            if action.get("type") != ACTION_SEND_PROMPT:
                raise ValueError("proposal action type does not match send_prompt")
            if target.get("session_key") != session_key:
                raise ValueError("proposal target does not match session_key")
            if parameters.get("text") != text:
                raise ValueError("prompt does not match approved proposal")
            existing = decision.get("execution") or {}
            if existing.get("request_id") == request_id:
                if existing.get("state") in {EXECUTION_SENT, EXECUTION_UNKNOWN}:
                    return existing
                if existing.get("state") == "ACCEPTED":
                    result = agent_decisions.append_execution_result(decision_id, {
                        "request_id": request_id,
                        "state": EXECUTION_UNKNOWN,
                        "reason": "host restarted after accepted write without receipt",
                    })
                    return result.get("execution") or result
            if decision["status"] != STATUS_APPROVED:
                raise PermissionError("prompt requires an approved proposal")
            accepted = agent_decisions.execute_approved(decision_id, proposal_hash, request_id)
            try:
                written = bool(session.write(prepared))
            except Exception:
                written = False
            if not written:
                result = agent_decisions.append_execution_result(decision_id, {
                    "request_id": request_id,
                    "state": EXECUTION_UNKNOWN,
                    "reason": "PTY write outcome is unknown",
                    "provider_processed": False,
                })
                return result.get("execution") or result
            result = agent_decisions.append_execution_result(decision_id, {
                "request_id": request_id,
                "state": EXECUTION_SENT,
                "result": "host_write_accepted",
                "provider_processed": False,
            })
            return result.get("execution") or accepted

    def get_all_sessions(self) -> list[dict]:
        result = []
        for s in list(self.sessions.values()):
            # State is keyed by the tab id (s.id); the hook writes the CURRENT claude
            # session_id as a field. If it differs from what we launched with, the user
            # swapped sessions inside the tab (/clear or /resume) — follow it so restore
            # and links target the live session, not the stale original.
            st = read_state(s.id)
            cur = st.get("session_id") if st else None
            if cur and cur != s.claude_session_id:
                try:
                    from terminal import work_items
                    work_items.migrate_link(s.claude_session_id, cur)
                except Exception:
                    pass
                s.claude_session_id = cur
            result.append(
                {
                    "id": s.id,
                    "group_name": s.group_name,
                    "path": s.path,
                    "is_alive": s.is_alive,
                    "claude_session_id": s.claude_session_id,
                    "agent_session_id": s.agent_session_id,
                    "session_key": s.session_key,
                    "terminal_type": s.terminal_type,
                    "state": st.get("status") if st else None,
                    "state_ts": st.get("ts") if st else None,
                }
            )
        return result

    def close_all(self):
        for session in list(self.sessions.values()):
            self.close_session(session.id)
        self.sessions.clear()
