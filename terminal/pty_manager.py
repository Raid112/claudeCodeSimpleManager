"""
Manages pywinpty pseudo-terminal instances for Claude Code sessions.

Each claude session is spawned with a session id we own (`--session-id <uuid>`)
and per-session hooks (`--settings <generated file>`). Those hooks report the
session's authoritative state (running/ready/tooluse/waiting) via terminal/hook_state.py,
which `get_all_sessions` reads back. There is no longer any need to poll the
~/.claude/projects directory to discover the session id.
"""

import os
import subprocess
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
from terminal import agent_decisions, ui_log
from terminal.input_protocol import prepare_composer_message, InputProtocolError
from terminal.session_registry import SessionRegistry
from terminal.session_support import (
    build_resume_command,
    build_powershell_command,
    find_codex_session_id,
    find_opencode_session_id,
    list_opencode_session_ids,
)


def build_child_env(base: dict, tab_id: str, session_key: str) -> str:
    """Build the null-separated environment block pywinpty's `spawn(env=...)` expects.

    pywinpty's `env` replaces the *whole* child environment (it is not overlaid on
    the parent's), so callers must seed `base` from `dict(os.environ)` to keep
    everything the child normally inherits (PATH, SystemRoot, ...). Only the two
    tab-identity vars are added on top. Windows also carries pseudo-variables whose
    NAME starts with `=` (e.g. `=C:`, `=ExitCode` — per-drive cwd tracking); these
    are not valid `name=value` pairs for CreateProcessW's env block and are dropped.
    Format per winpty.PTY.spawn's docstring: each `name=value` pair separated by a
    single `\0`, plus a trailing `\0`.
    """
    merged = dict(base)
    merged["CLAUDEMANAGER_TAB"] = tab_id
    merged["CLAUDEMANAGER_SESSION_KEY"] = session_key
    entries = [f"{k}={v}" for k, v in merged.items() if not k.startswith("=")]
    return "\0".join(entries) + "\0"


def _codex_sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def _opencode_database_path() -> Path:
    data_root = os.environ.get("XDG_DATA_HOME")
    if data_root:
        return Path(data_root) / "opencode" / "opencode.db"
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _kill_tree(pty) -> None:
    """Force-kill the PTY's powershell and everything under it (claude, node, MCP
    servers). Writing "exit" like PtySession.kill() does would just be typed into claude."""
    try:
        pid = pty.pid
    except Exception:
        return
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


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
        self.hooks_settings_path = hooks_settings_path
        # True only while restart() swaps the PTY: the ws reader polls is_alive between
        # reads, and the old process dying mid-swap must not close the tab's websocket.
        self._respawning = False
        self.pty = self._spawn(cols, rows, continue_session, resume)

    def _spawn(self, cols: int, rows: int, continue_session: bool, resume: bool):
        pty = winpty.PTY(cols, rows)
        cmdline = build_resume_command(
            self.terminal_type,
            session_id=(self.claude_session_id if self.terminal_type == "claude" else self.agent_session_id),
            continue_session=continue_session,
            hooks_path=self.hooks_settings_path,
            resume_session=resume,
        )

        # Tag the child env with this tab's id (and the stable session_key) so the
        # Claude Code hooks (grandchildren of powershell) can report WHICH tab they
        # belong to, independent of the session_id they carry — which the user can
        # swap via /clear or /resume. Passed explicitly per spawn rather than
        # mutated on os.environ: create_session runs on whichever thread calls it
        # (the pywebview bridge thread for UI-initiated sessions, the
        # ThreadingHTTPServer handler thread for agent_gateway-initiated ones), so
        # two concurrent creations mutating the same global os.environ would race
        # and cross-assign tab ids between sessions.
        env_block = build_child_env(os.environ, self.id, self.session_key)
        powershell_cmdline = build_powershell_command(cmdline)
        pty.spawn(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmdline=powershell_cmdline,
            cwd=self.path,
            env=env_block,
        )
        return pty

    def restart(self, cols: int, rows: int):
        """Kill the provider process and start it again under the SAME tab id and
        session_key, resuming the current conversation. For a CLI that got slow or
        stuck: the tab, its websocket and its work-item link all survive; only the
        process underneath is new. Kill before spawn so two claudes never write the
        same session transcript at once."""
        self._respawning = True
        try:
            _kill_tree(self.pty)
            delete_state(self.id)  # the new process is idle until its first hook
            resume_id = self.claude_session_id if self.terminal_type == "claude" else self.agent_session_id
            self.pty = self._spawn(
                cols, rows,
                continue_session=(self.terminal_type == "claude" and not resume_id),
                resume=bool(resume_id),
            )
            ui_log.log("pty.restart", src="py", session=self.id, cols=cols, rows=rows,
                       resumed=bool(resume_id))
        finally:
            self._respawning = False

    @property
    def is_alive(self) -> bool:
        if self._respawning:
            return True
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
        """Tell the PTY its new size. A FAILURE HERE IS INVISIBLE to the user: xterm already
        reflowed to `cols`x`rows`, the child process keeps the old size, and the screen stays
        broken until some later resize happens to succeed (switching tabs, minimizing) --
        the exact symptom being chased. Still best-effort, but no longer silent: both
        outcomes land in the UI log next to the frontend's own resize records."""
        try:
            self.pty.set_size(cols, rows)
            ui_log.log("pty.resize", src="py", session=self.id, cols=cols, rows=rows)
        except Exception as exc:
            ui_log.log("pty.resize.failed", src="py", session=self.id, cols=cols, rows=rows,
                       error=f"{type(exc).__name__}: {exc}")

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
        # process_exited producer (Hermes plan Task 4): report-once set + its
        # own lock, guarding get_all_sessions() against double-emitting when
        # the pywebview poll thread and an agent_gateway handler thread call
        # it concurrently for the same dead session.
        self._exit_reported: set[str] = set()
        self._exit_report_lock = threading.Lock()

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

    def restart_session(self, session_id: str, cols: int, rows: int) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        with self.registry.lock_for(session.session_key):
            session.restart(cols, rows)
        return True

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
            with self._exit_report_lock:
                # session_key is a fresh uuid4 per spawn (never reused), so this
                # is just hygiene against an unbounded set over the app's lifetime,
                # not a correctness requirement.
                self._exit_reported.discard(session.session_key)

    def _maybe_report_exit(self, session: "PtySession", alive: bool) -> None:
        """Emit exactly one ``process_exited`` event for an UNEXPECTED PTY
        alive->dead transition (Hermes plan Task 4). Called from
        ``get_all_sessions``, which is polled every 2s from the pywebview UI
        thread AND called from the agent_gateway's ThreadingHTTPServer handler
        threads -- so this must never raise, and two concurrent callers seeing
        the same dead session must still only produce one event.

        An intentional close (``close_session`` / ``close_all``, both routed
        through ``registry.begin_close``) removes the session from
        ``self.sessions`` before the PTY is ever touched, so the closing
        thread itself never observes its own session here. The remaining race
        is a concurrent ``get_all_sessions`` call that snapshotted
        ``self.sessions`` (see the ``list(...)`` in that method) just before
        the removal, then evaluates ``is_alive`` after the close already
        began -- ``registry.is_closing`` closes exactly that window.
        """
        if alive:
            return
        key = session.session_key
        try:
            with self._exit_report_lock:
                if key in self._exit_reported or self.registry.is_closing(key):
                    return
                self._exit_reported.add(key)
            from terminal import work_items
            from terminal.hermes_events import PROCESS_EXITED_EVENT
            work_items.log_event(key, PROCESS_EXITED_EVENT, claude_session_id=session.claude_session_id)
        except Exception:
            pass

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
                    # Aba claude vinculada antes de ter session_id foi gravada pela
                    # session_key (app.js sessionLinkKeyOf); sem isso o vínculo se perdia.
                    old = s.claude_session_id or (
                        s.session_key if s.terminal_type == "claude" else None)
                    work_items.migrate_link(old, cur)
                except Exception:
                    pass
                s.claude_session_id = cur
            alive = s.is_alive
            self._maybe_report_exit(s, alive)
            result.append(
                {
                    "id": s.id,
                    "group_name": s.group_name,
                    "path": s.path,
                    "is_alive": alive,
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
