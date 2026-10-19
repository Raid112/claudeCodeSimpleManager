"""
pywebview JS API bridge — exposes Python functions to JavaScript.
"""

import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
import webbrowser
import webview
from pathlib import Path
from terminal.pty_manager import PtyManager
from terminal.ws_server import WebSocketServer
from terminal import work_items, jira_client, teams_graph
from terminal import agent_decisions
from terminal import agent_contracts
from terminal import hermes_events
from terminal import ui_log
from terminal.agent_contracts import ACTION_SEND_PROMPT, EXECUTION_SENT, EXECUTION_UNKNOWN
from terminal.input_protocol import InputProtocolError

# How often poll_wake may actually hit Graph. Matches teams_graph's own cache TTL: a
# shorter interval would only re-read the same cached chats.
WAKE_POLL_SECONDS = 120

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
SESSIONS_PATH = Path(__file__).parent.parent / "sessions.json"


# Pre-write dispatch failures where retrying can actually succeed once the
# operator fixes the world. Every other reason is permanent for this decision and
# needs a replan, not a retry (see _receipt_after_dispatch_failure).
_RETRYABLE_DISPATCH_REASONS = frozenset({
    "session_not_found",      # reopen the tab, then retry
    "emergency_stop_active",  # unlock, then retry
    "unexpected_error",       # unclassified: allow one retry rather than dead-end
})

DEFAULT_LAYOUT = {"split_ratio": 0.6, "composer_collapsed": False,
                  "sidebar_width": 260, "locked_wi_id": None}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {"terminal_type": "claude", "groups": []}
    # Backfill defaults for forward-compat
    if "layout" not in config:
        config["layout"] = dict(DEFAULT_LAYOUT)
    else:
        for k, v in DEFAULT_LAYOUT.items():
            config["layout"].setdefault(k, v)
    return config


def _save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _load_sessions() -> dict:
    if SESSIONS_PATH.exists():
        try:
            with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"tabs": [], "active_tab_index": 0}
    return {"tabs": [], "active_tab_index": 0}


def _save_sessions(sessions: dict):
    with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)


class Bridge:
    """Exposed to JS as window.pywebview.api"""

    # Fixed namespace for deriving the host dispatch request_id (see
    # _host_dispatch_request_id). Arbitrary but must never change, or a retry
    # issued after an app upgrade would mint a different request_id than the
    # one already recorded for an in-flight decision.
    _REQUEST_ID_NAMESPACE = uuid.UUID("d9f6a9b2-8b39-4a6b-9b0a-9a8f8e6d2c31")

    def __init__(self, pty_manager: PtyManager, ws_server: WebSocketServer):
        self.pty_manager = pty_manager
        self.ws_server = ws_server
        self._window: webview.Window | None = None
        self._toaster = None  # lazily created WindowsToaster (windows_toasts)
        self._operator_capabilities: dict[str, dict] = {}
        # One live token per decision: reissuing discards the previous one instead
        # of accumulating (see _issue_operator_capability). This is what actually
        # bounds the map to O(pending decisions) instead of the old ~30/min-per-
        # decision leak; the expiry purge on top of it only cleans up the tail end
        # after a decision stops being polled at all (rejected/executed).
        self._capability_by_decision: dict[str, str] = {}
        self._emergency_capability = secrets.token_urlsafe(32)
        self._hermes_publisher = None  # set by main.py; see set_hermes_publisher
        self._last_wake_poll = 0.0     # see poll_wake

    def set_window(self, window: webview.Window):
        self._window = window

    def set_hermes_publisher(self, publisher):
        """Hold the wake-up publisher for read-only status exposure (see
        get_hermes_publisher_status). None means it never started."""
        self._hermes_publisher = publisher

    def get_hermes_publisher_status(self) -> dict:
        """Read-only publisher state for the UI.

        A publisher that disabled itself on 401/403/404 will never recover on its
        own, so this is the only place an operator can find out. `state: "off"`
        means it was never started (no secret in secrets.json -- the intended off
        switch), which is not an error."""
        publisher = self._hermes_publisher
        if publisher is None:
            return {"state": "off", "reason": "not_configured"}
        try:
            return publisher.status()
        except Exception as exc:
            return {"state": "unknown", "reason": type(exc).__name__}

    def notify_state(self, session_id: str, tab_name: str, state: str):
        """Show a Windows toast for a backgrounded session transition.

        Called from the frontend only when the window is unfocused (see
        app.js _maybePlayStateSound). Clicking the toast brings the window
        forward and switches to the originating tab. Best-effort: a missing
        windows_toasts dependency or any toast error must never break the app.
        """
        try:
            from windows_toasts import WindowsToaster, Toast

            if self._toaster is None:
                self._toaster = WindowsToaster("Claude Manager")

            title = "Sessão pronta" if state == "ready" else "Aguardando permissão"
            toast = Toast()
            toast.text_fields = [title, tab_name or "Claude"]
            toast.on_activated = lambda _args=None, sid=session_id: self._focus_tab(sid)
            self._toaster.show_toast(toast)
        except Exception as e:
            print(f"notify_state failed (toast skipped): {e}")

    def ui_log(self, records: list) -> int:
        """Append a batch of frontend diagnostics (see web/js/uilog.js). Returns how many
        lines were written. Never raises: the log must never be why the UI breaks."""
        try:
            return ui_log.append(records or [], src="ui")
        except Exception:
            return 0

    def open_logs_dir(self) -> str:
        r"""Open %LOCALAPPDATA%\ClaudeManager\logs in the Explorer and return its path."""
        try:
            path = ui_log.log_dir()
            os.startfile(str(path))
            return str(path)
        except Exception as exc:
            print(f"open_logs_dir failed: {exc}")
            return ""

    def notify_wake(self, person: str, title: str):
        """Toast for a parked item that came back on its own because the other side
        answered (see main.py's wake poll). Best-effort, exactly like notify_state."""
        try:
            from windows_toasts import WindowsToaster, Toast

            if self._toaster is None:
                self._toaster = WindowsToaster("Claude Manager")

            toast = Toast()
            toast.text_fields = [f"{person or 'Alguém'} respondeu", title or ""]
            toast.on_activated = lambda _args=None: self._focus_window()
            self._toaster.show_toast(toast)
        except Exception as e:
            print(f"notify_wake failed (toast skipped): {e}")

    def _focus_tab(self, session_id: str):
        """Bring the window to the foreground and select the given tab.

        Runs on the windows_toasts activation thread. The on_top toggle is the
        standard pywebview trick to force the window to the foreground on Windows.
        """
        if not self._focus_window():
            return
        try:
            self._window.evaluate_js(
                f"window.app && window.app.switchToTerminal({json.dumps(session_id)})")
        except Exception as e:
            print(f"_focus_tab failed: {e}")

    def _focus_window(self) -> bool:
        """Bring the window forward (the on_top toggle is the pywebview trick for it).
        False when there is no window or the call failed."""
        win = self._window
        if win is None:
            return False
        try:
            win.restore()
            win.on_top = True
            win.on_top = False
            return True
        except Exception as e:
            print(f"_focus_window failed: {e}")
            return False

    def get_groups(self) -> list[dict]:
        config = _load_config()
        return config.get("groups", [])

    def get_terminal_type(self) -> str:
        config = _load_config()
        return config.get("terminal_type", "claude")

    def set_terminal_type(self, terminal_type: str):
        config = _load_config()
        config["terminal_type"] = terminal_type
        _save_config(config)

    def get_layout(self) -> dict:
        config = _load_config()
        return config.get("layout", dict(DEFAULT_LAYOUT))

    def set_layout(self, layout: dict):
        config = _load_config()
        current = config.get("layout", dict(DEFAULT_LAYOUT))
        if "split_ratio" in layout:
            ratio = float(layout["split_ratio"])
            current["split_ratio"] = max(0.1, min(0.95, ratio))
        if "composer_collapsed" in layout:
            current["composer_collapsed"] = bool(layout["composer_collapsed"])
        if "sidebar_width" in layout:
            current["sidebar_width"] = max(180, min(600, int(layout["sidebar_width"])))
        # Work item "travado": só as abas dele ficam expandidas na barra. None = nada travado.
        if "locked_wi_id" in layout:
            wi = layout["locked_wi_id"]
            current["locked_wi_id"] = str(wi) if wi else None
        config["layout"] = current
        _save_config(config)

    def get_composer_history(self) -> list:
        config = _load_config()
        return config.get("composer_history", [])

    def set_composer_history(self, history: list):
        config = _load_config()
        # Cap at 50 most recent
        if isinstance(history, list):
            config["composer_history"] = [str(x) for x in history[-50:]]
        _save_config(config)

    def add_group(self) -> dict | None:
        if not self._window:
            return None
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=str(Path.home()),
        )
        if result and len(result) > 0:
            folder = result[0]
            name = Path(folder).name
            config = _load_config()
            # Check duplicate
            if any(g["name"] == name for g in config["groups"]):
                return None
            group = {"name": name, "path": folder.replace("\\", "/")}
            config["groups"].append(group)
            _save_config(config)
            return group
        return None

    def remove_group(self, name: str):
        config = _load_config()
        config["groups"] = [g for g in config["groups"] if g["name"] != name]
        _save_config(config)
        # Close all terminals for this group
        for s in list(self.pty_manager.sessions.values()):
            if s.group_name == name:
                self.ws_server.revoke_session(s.id)
                self.pty_manager.close_session(s.id)

    def open_terminal(
        self,
        group_name: str,
        path: str,
        cols: int = 120,
        rows: int = 30,
        terminal_type: str = None,
        continue_session: bool = False,
        claude_session_id: str = None,
        agent_session_id: str = None,
        session_key: str = None,
    ) -> dict:
        if terminal_type is None:
            terminal_type = self.get_terminal_type()
        session = self.pty_manager.create_session(
            group_name,
            path,
            cols,
            rows,
            terminal_type=terminal_type,
            continue_session=continue_session,
            claude_session_id=claude_session_id,
            agent_session_id=agent_session_id,
            session_key=session_key,
        )
        return {
            "session_id": session.id,
            "ws_port": self.ws_server.actual_port,
            "ws_capability": self.ws_server.issue_capability(session.id),
            "group_name": group_name,
            "path": path,
            "terminal_type": terminal_type,
            # Claude's provider ID is known at spawn; Codex/OpenCode IDs may be
            # discovered asynchronously, while session_key is always immediate.
            "claude_session_id": session.claude_session_id,
            "agent_session_id": session.agent_session_id,
            "session_key": session.session_key,
        }

    def restart_terminal(self, session_id: str, cols: int = 120, rows: int = 30) -> bool:
        """Respawn a slow/stuck provider process in place (same tab, resumed session)."""
        return self.pty_manager.restart_session(session_id, cols, rows)

    def close_terminal(self, session_id: str):
        self.ws_server.revoke_session(session_id)
        self.pty_manager.close_session(session_id)

    def get_terminals(self) -> list[dict]:
        return self.pty_manager.get_all_sessions()

    def issue_ws_capability(self, session_id: str) -> str | None:
        """Issue one fresh WebSocket capability for a still-live managed tab."""
        session = self.pty_manager.get_session(session_id)
        if session is None or not session.is_alive:
            return None
        return self.ws_server.issue_capability(session_id)

    def get_ws_port(self) -> int:
        return self.ws_server.actual_port

    # ---------- Hermes proposal review (UI-only authority) ----------
    def _purge_expired_capabilities(self) -> None:
        """Drop capability entries whose expiry has passed.

        Only reissuing a token discards the *previous* token, so a decision that
        stops being polled (rejected, executed, or the panel simply not open for a
        while) leaves its last-issued token stranded past its own expires_at with
        nothing left to supersede it. This sweep is what reclaims those, on top of
        the per-decision single-token invariant in _issue_operator_capability.
        """
        now = time.time()
        expired = [token for token, record in self._operator_capabilities.items()
                  if record["expires_at"] <= now]
        for token in expired:
            del self._operator_capabilities[token]
        for decision_id, token in list(self._capability_by_decision.items()):
            if token not in self._operator_capabilities:
                del self._capability_by_decision[decision_id]

    def _issue_operator_capability(self, decision_id: str, proposal_hash: str) -> str:
        """Issue the one live capability token for this decision.

        Reissuing (every poll, so the approve/retry button always closes over a
        fresh token) discards whatever token was live before for the same
        decision_id -- this is the actual bound on growth (O(pending decisions)
        steady-state), not just the expiry sweep, which only mops up the tail end
        for a decision that stops being polled altogether. Because the previous
        token is invalidated immediately, callers must always read the current
        operator_capability off the latest fetched decision at click time, never
        off a value captured earlier (see agent-approvals.js card patching).
        """
        self._purge_expired_capabilities()
        old_token = self._capability_by_decision.get(decision_id)
        if old_token is not None:
            self._operator_capabilities.pop(old_token, None)
        token = secrets.token_urlsafe(32)
        self._operator_capabilities[token] = {
            "decision_id": decision_id,
            "proposal_hash": proposal_hash,
            "expires_at": time.time() + 120,
        }
        self._capability_by_decision[decision_id] = token
        return token

    def _consume_operator_capability(self, token: str, decision_id: str, proposal_hash: str) -> bool:
        record = self._operator_capabilities.get(token)
        if not record or record["expires_at"] <= time.time():
            return False
        if record["decision_id"] != decision_id or record["proposal_hash"] != proposal_hash:
            return False
        # Consumed exactly once: delete rather than flag "used", so a capability
        # can never be found valid twice and never lingers in the map either.
        del self._operator_capabilities[token]
        if self._capability_by_decision.get(decision_id) == token:
            del self._capability_by_decision[decision_id]
        return True

    def _session_label(self, session_key: str | None) -> str | None:
        """Human-readable tab identity for a decision's target session.

        Best-effort only: a closed tab or a session_key the registry no longer
        knows about must degrade to None, never raise -- the decision itself is
        still worth showing to the operator even if we cannot name its tab.
        """
        if not session_key:
            return None
        try:
            find_session = getattr(self.pty_manager, "find_by_session_key", None)
            session = find_session(session_key) if find_session else None
        except Exception:
            return None
        if session is None:
            return None
        try:
            # Matches app.js getDisplayName's default ("<group> #<id[:4]>"); a
            # frontend-side custom tab name is not visible here.
            return f"{session.group_name} #{str(session.id)[:4]}"
        except Exception:
            return None

    @staticmethod
    def _resolve_work_item(store: dict, session_key: str | None,
                           find_session) -> tuple[str | None, str]:
        """Resolve the linked work item for a decision's target session.

        session_links is keyed by claude_session_id for claude tabs and by
        session_key for every other provider (see app.js sessionLinkKeyOf) --
        try session_key first since that is what a send_prompt proposal always
        carries, then fall back to the live session's claude_session_id if the
        tab is still open. Any failure (closed tab, malformed store) degrades to
        (None, ""), never raises.
        """
        if not session_key:
            return None, ""
        try:
            links = store.get("session_links") or {}
            link = links.get(session_key)
            if link is None:
                session = find_session(session_key)
                csid = getattr(session, "claude_session_id", None) if session else None
                if csid:
                    link = links.get(csid)
            if not link or not link.get("wi_id"):
                return None, ""
            wi_id = link["wi_id"]
            item = next((it for it in store.get("items", []) if it.get("id") == wi_id), None)
            return wi_id, ((item or {}).get("title") or "")
        except Exception:
            return None, ""

    def list_pending_agent_decisions(self) -> list[dict]:
        """Display-ready view model for the approval UI.

        Each item is derived from one raw journal decision (kept intact under
        "raw" so nothing the journal knows is lost) plus two defensive joins --
        session label and work item -- resolved via pty_manager/work_items and
        degraded to null/"" rather than raised on any lookup failure. The prompt
        text reaches here unredacted already (agent_decisions carries it plainly;
        only the Hermes-facing gateway redacts), so this is just projection, not
        a new read path.
        """
        pending = agent_decisions.list_pending_decisions()
        try:
            store = work_items.load_store()
        except Exception:
            store = {"items": [], "session_links": {}}
        # A fixture pty_manager in a test may not implement find_by_session_key at
        # all; degrade to "no session found" rather than let the attribute lookup
        # itself raise outside _session_label/_resolve_work_item's own try/except.
        find_session = getattr(self.pty_manager, "find_by_session_key", lambda _key: None)
        items = []
        for state in pending:
            proposal = state.get("proposal") or {}
            action = proposal.get("action") or {}
            target = action.get("target") or {}
            parameters = action.get("parameters") or {}
            risk = proposal.get("risk") or {}
            session_key = target.get("session_key")
            prompt_text = parameters.get("text") or ""
            wi_id, wi_title = self._resolve_work_item(store, session_key, find_session)
            items.append({
                "decision_id": state["decision_id"],
                "proposal_hash": state["proposal_hash"],
                "pending_reason": state.get("pending_reason"),
                "prompt_text": prompt_text,
                "prompt_length": len(prompt_text),
                "intent": proposal.get("intent"),
                "expected_outcome": action.get("expected_outcome"),
                "risk_level": risk.get("level"),
                "expires_at": state.get("expires_at"),
                "version": state.get("version"),
                "parent_decision_id": state.get("parent_decision_id"),
                "trace_id": proposal.get("trace_id"),
                "session_key": session_key,
                "session_label": self._session_label(session_key),
                "work_item_id": wi_id,
                "work_item_title": wi_title,
                "operator_capability": self._issue_operator_capability(
                    state["decision_id"], state["proposal_hash"]),
                "raw": state,
            })
        return items

    def verify_proposal_hash(self, decision_id: str, rendered_text: str | None = None) -> dict:
        """Recompute the proposal hash in Python and report the verdict.

        Recomputing agent_contracts.proposal_hash in JS would mean reproducing
        json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":")) with
        the proposal_hash/status pop -- fragile with unicode (review finding #9).
        This is the one place that comparison happens.

        ``found`` is separate from ``hash_ok`` on purpose: a decision remains
        gettable by id forever (the journal never forgets), but the UI calls this
        for cards it still has cached even after the decision left the pending
        list, and a transient lookup miss must never render as "hash mismatch".
        Only found and (not hash_ok) is a genuine integrity warning.
        """
        try:
            decision = agent_decisions.get_decision(decision_id)
        except Exception:
            decision = None
        if decision is None:
            return {"found": False, "hash_ok": False, "text_ok": None,
                    "prompt_length": 0, "proposal_hash": None}
        proposal = decision.get("proposal") or {}
        try:
            recomputed = agent_contracts.proposal_hash(proposal)
        except Exception:
            recomputed = None
        recorded_hash = decision.get("proposal_hash")
        hash_ok = recomputed is not None and recomputed == recorded_hash
        action = proposal.get("action") or {}
        parameters = action.get("parameters") or {}
        approved_text = parameters.get("text") or ""
        text_ok = None if rendered_text is None else (rendered_text == approved_text)
        return {
            "found": True,
            "hash_ok": hash_ok,
            "text_ok": text_ok,
            "prompt_length": len(approved_text),
            "proposal_hash": recorded_hash,
        }

    def approve_agent_proposal(self, decision_id: str, proposal_hash: str,
                               reviewer_id: str, capability: str) -> dict:
        """Approve one proposal and, for send_prompt, dispatch it immediately.

        Approval *is* the trigger now (Task 2): Hermes cannot reach the PTY
        write through any MCP tool, so if the approved action is send_prompt
        the host writes it synchronously, on this same call, and returns a
        receipt instead of the bare decision. Dispatch takes the registry lock
        plus the journal file lock and re-reads the whole hash chain, so this
        runs on the pywebview bridge thread with a visible pending state in the
        UI; move it to a worker with receipt polling only if that latency ever
        becomes noticeable in practice.

        Exceptions from the approval step itself (invalid/expired capability,
        wrong status, hash mismatch) still raise exactly as before -- only a
        failure at or after dispatch is translated into a WRITE_FAILED receipt
        instead of an exception.
        """
        if not self._consume_operator_capability(capability, decision_id, proposal_hash):
            raise PermissionError("invalid or expired operator capability")
        decision = agent_decisions.approve(decision_id, proposal_hash, reviewer_id, capability)
        action_type = ((decision.get("proposal") or {}).get("action") or {}).get("type")
        if action_type != ACTION_SEND_PROMPT:
            # Defensive only: ALLOWED_ACTIONS is currently exactly {send_prompt},
            # so every approvable decision reaches here. Kept in case that ever
            # changes so a future action type fails safe instead of dispatching.
            return decision
        request_id = self._host_dispatch_request_id(decision_id, proposal_hash)
        return self._prompt_dispatch_receipt(decision, request_id)

    def retry_prompt_dispatch(self, decision_id: str, proposal_hash: str, capability: str) -> dict:
        """Re-attempt dispatch of an already-approved send_prompt decision.

        Only reachable for a decision list_pending_decisions still tags
        ``awaiting_dispatch`` (approved, no terminal execution recorded) -- a
        pre-write failure left nothing in the journal, so a retry is safe. A
        fresh operator capability is required: an approved-but-undispatched
        decision must not become a capability-free write path just because the
        approval already happened. Reuses the same deterministic request_id as
        the original approval, so pty_manager.send_prompt's idempotent replay
        path recognizes this as the same execution attempt rather than a
        second, conflicting one.
        """
        if not self._consume_operator_capability(capability, decision_id, proposal_hash):
            raise PermissionError("invalid or expired operator capability")
        decision = agent_decisions.get_decision(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        if decision["proposal_hash"] != proposal_hash:
            raise PermissionError("proposal hash does not match")
        action_type = ((decision.get("proposal") or {}).get("action") or {}).get("type")
        if action_type != ACTION_SEND_PROMPT:
            raise ValueError("retry_prompt_dispatch only applies to send_prompt decisions")
        request_id = self._host_dispatch_request_id(decision_id, proposal_hash)
        return self._prompt_dispatch_receipt(decision, request_id)

    def _host_dispatch_request_id(self, decision_id: str, proposal_hash_value: str) -> str:
        """Derive the host's dispatch request_id deterministically from the decision.

        uuid5 is a pure function of its inputs, so the same (decision_id,
        proposal_hash) pair always yields the same request_id -- across a retry
        after a pre-write failure, and across an app restart -- with nothing to
        persist. This is what lets a retry hit pty_manager.send_prompt's
        idempotent fast path instead of tripping execute_approved's "decision
        already has another execution request" ConflictError.
        """
        return str(uuid.uuid5(self._REQUEST_ID_NAMESPACE, f"{decision_id}:{proposal_hash_value}"))

    def _prompt_dispatch_receipt(self, decision: dict, request_id: str) -> dict:
        decision_id = decision["decision_id"]
        action = decision["proposal"].get("action") or {}
        target = action.get("target") or {}
        parameters = action.get("parameters") or {}
        session_key = target.get("session_key")
        text = parameters.get("text")
        try:
            execution = self.pty_manager.send_prompt(
                session_key, text, request_id, decision_id, decision["proposal_hash"])
        except Exception as exc:  # noqa: BLE001 - translated into a receipt, never re-raised
            return self._receipt_after_dispatch_failure(decision_id, request_id, exc)
        return self._execution_receipt(decision_id, request_id, execution)

    def _receipt_after_dispatch_failure(self, decision_id: str, request_id: str,
                                        exc: Exception) -> dict:
        """Translate a send_prompt exception into a receipt by re-reading the
        journal instead of trusting the exception type.

        The exception type only supplies a human-readable ``reason``. Whether
        the decision is safely retryable is decided by what actually landed in
        the hash chain: an exception raised AFTER the PTY write (e.g.
        append_execution_result failing on a full disk) must never be reported
        as "nothing happened, retry safely" -- that risks a duplicate prompt
        landing in the terminal on retry.
        """
        reason = self._dispatch_failure_reason(exc)
        try:
            decision = agent_decisions.get_decision(decision_id)
        except Exception:
            # Cannot even confirm what happened. Fail closed: never claim retryable.
            return {
                "decision_id": decision_id, "request_id": request_id,
                "state": "PROVIDER_PROCESSING_UNKNOWN", "retryable": False, "reason": reason,
            }
        execution = (decision or {}).get("execution")
        if not execution:
            # execute_approved raised before any journal append: the write
            # boundary was never crossed, so nothing happened. Retry is *safe*,
            # but only worth offering when the operator can change the world and
            # try again (reopen the tab, clear the emergency stop). An expired or
            # mismatched proposal, a text the input protocol refuses, or a
            # non-conversational tab can never succeed on retry — offering the
            # button there just teaches the operator to click a dead control.
            return {
                "decision_id": decision_id, "request_id": request_id,
                "state": "WRITE_FAILED",
                "retryable": reason in _RETRYABLE_DISPATCH_REASONS,
                "reason": reason,
            }
        if execution.get("state") in (EXECUTION_SENT, EXECUTION_UNKNOWN):
            return self._execution_receipt(decision_id, request_id, execution)
        # ACCEPTED (or any other non-terminal state) reached the journal, so the
        # write boundary was crossed but the outcome was never recorded -- the
        # same honest "we don't know" as pty_manager's own restart-recovery path.
        return {
            "decision_id": decision_id, "request_id": execution.get("request_id", request_id),
            "state": "PROVIDER_PROCESSING_UNKNOWN", "retryable": False, "reason": reason,
        }

    @staticmethod
    def _dispatch_failure_reason(exc: Exception) -> str:
        """Human-readable reason code for display only -- never used to decide
        retryable (see _receipt_after_dispatch_failure)."""
        if isinstance(exc, KeyError):
            return "session_not_found"
        if isinstance(exc, InputProtocolError):  # subclass of ValueError: check first
            return "invalid_input"
        if isinstance(exc, PermissionError):
            return "unsupported_provider" if "PowerShell" in str(exc) else "not_approved"
        if isinstance(exc, agent_decisions.AuthorizationError):
            return "emergency_stop_active"
        if isinstance(exc, agent_decisions.ConflictError):
            return "execution_conflict"
        if isinstance(exc, ValueError):
            return "proposal_expired" if str(exc) == "proposal_expired" else "proposal_mismatch"
        return "unexpected_error"

    @staticmethod
    def _execution_receipt(decision_id: str, request_id: str, execution: dict) -> dict:
        state = execution.get("state")
        if state == EXECUTION_SENT:
            mapped = "HOST_WRITE_ACCEPTED"
        elif state == EXECUTION_UNKNOWN:
            mapped = "PROVIDER_PROCESSING_UNKNOWN"
        else:
            # ACCEPTED reaching here would mean send_prompt returned without ever
            # calling append_execution_result, which its own code never does (it
            # always finishes at SENT/UNKNOWN or raises). Fail closed rather than
            # claim a write was accepted.
            mapped = "PROVIDER_PROCESSING_UNKNOWN"
        return {
            "decision_id": decision_id, "request_id": execution.get("request_id", request_id),
            "state": mapped, "retryable": False, "execution": execution,
        }

    def reject_agent_proposal(self, decision_id: str, proposal_hash: str, reviewer_id: str,
                              capability: str, reason_code: str, comment: str,
                              requested_changes: list[str] | None = None,
                              verdict: str = "reject", scope: str = "this_proposal") -> dict:
        if not self._consume_operator_capability(capability, decision_id, proposal_hash):
            raise PermissionError("invalid or expired operator capability")
        feedback = {
            "feedback_id": str(uuid.uuid4()),
            "decision_id": decision_id,
            "reviewer_id": reviewer_id,
            "verdict": verdict,
            "reason_code": reason_code,
            "comment": comment,
            "scope": scope,
            "requested_changes": requested_changes or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return agent_decisions.record_feedback(decision_id, feedback)

    def get_emergency_capability(self) -> str:
        return self._emergency_capability

    def get_agent_control_state(self) -> dict:
        return agent_decisions.control_state()

    def emergency_stop(self, capability: str) -> bool:
        if not secrets.compare_digest(str(capability), self._emergency_capability):
            return False
        self.ws_server.revoke_all()
        agent_decisions.set_emergency_stop("local-user", "bridge-ui")
        return True

    def unlock_emergency_stop(self, capability: str) -> bool:
        if not secrets.compare_digest(str(capability), self._emergency_capability):
            return False
        return not agent_decisions.clear_emergency_stop("local-user", "bridge-ui")["emergency_stopped"]

    def save_sessions(self, tabs: list, active_tab_index: int = 0):
        _save_sessions({"tabs": tabs, "active_tab_index": active_tab_index})

    def load_sessions(self) -> dict:
        return _load_sessions()

    def clear_sessions(self):
        _save_sessions({"tabs": [], "active_tab_index": 0})

    def save_sessions_from_backend(self):
        tabs = []
        for i, session in enumerate(self.pty_manager.sessions.values()):
            tabs.append(
                {
                    "group_name": session.group_name,
                    "path": session.path,
                    "tab_order": i,
                    "claude_session_id": session.claude_session_id,
                    "agent_session_id": session.agent_session_id,
                    "session_key": session.session_key,
                    "terminal_type": session.terminal_type,
                }
            )
        _save_sessions({"tabs": tabs, "active_tab_index": 0})

    def open_url(self, url: str):
        webbrowser.open(url)

    # ---------- work items (batch 1: local store only, no Jira/Teams yet) ----------
    # Thin wrappers over terminal/work_items. The session<->item link lives in
    # work_items.json (session_links), keyed by the stable provider/tab key — deliberately NOT
    # in sessions.json, which the frontend rewrites wholesale every 10s.
    def list_work_items(self) -> dict:
        """Full store: {version, items[], session_links{}}. One call feeds the whole panel."""
        return work_items.load_store()

    def create_work_item(self, source: str, title: str, external_key: str = None,
                         external_url: str = None, status: str = None,
                         duedate: str = None, duedate_has_time: bool = False,
                         person: str = None) -> dict:
        return work_items.new_item(source, title, external_key=external_key,
                                   external_url=external_url, status=status,
                                   duedate=duedate, duedate_has_time=duedate_has_time,
                                   person=person)

    def rename_work_item(self, wi_id: str, title: str) -> dict:
        return work_items.rename_item(wi_id, title)

    def complete_work_item(self, wi_id: str):
        work_items.complete_item(wi_id)

    def reopen_work_item(self, wi_id: str) -> dict | None:
        return work_items.reopen_item(wi_id)

    def set_work_item_waiting(self, wi_id: str, waiting: bool = True) -> dict | None:
        return work_items.set_waiting(wi_id, waiting)

    def poll_wake(self) -> list:
        """Unpark items whose Teams chat got a reply (terminal/work_items.wake_on_reply).

        Driven by the frontend's own refresh tick rather than a background timer, on purpose:
        the store is rewritten whole on every save, so a timer thread writing it unattended
        every couple of minutes can bury a change the user just made. Coming in through the
        bridge puts this on the same path as every other mutation.

        Throttled to WAKE_POLL_SECONDS, and Graph is skipped entirely when nothing teams-ish
        is parked — an idle queue costs one list comprehension. Never raises.
        Returns the items that came back so the caller can refresh."""
        now = time.time()
        if now - self._last_wake_poll < WAKE_POLL_SECONDS:
            return []
        self._last_wake_poll = now
        try:
            parked = [it for it in work_items.list_items()
                      if not it.get("done")
                      and it.get("workflow_state") == "waiting"
                      and work_items.teams_chat_id(it)]
            if not parked:
                return []
            woken = work_items.wake_on_reply(teams_graph.list_recent())
        except Exception as exc:
            print(f"[wake] poll failed: {exc}")
            return []
        for item in woken:
            self.notify_wake(item.get("woke_by"), item.get("title"))
        return [{"wi_id": it["id"], "person": it.get("woke_by"), "title": it.get("title")}
                for it in woken]

    def ack_work_item_wake(self, wi_id: str):
        """Dismiss the "they answered" badge after the user opened the item."""
        work_items.ack_wake(wi_id)

    def reorder_work_items(self, ordered_ids: list):
        work_items.reorder(ordered_ids)

    def link_session(self, session_key: str, wi_id: str,
                     group_name: str = None, path: str = None, name: str = None):
        work_items.link(session_key, wi_id, group_name=group_name, path=path, name=name)

    def unlink_session(self, session_key: str):
        work_items.unlink(session_key)

    def work_daily_digest(self, days: int = 2) -> dict:
        """Operational recap: today/yesterday plus active waiting items."""
        return work_items.work_overview(days)

    def work_item_agent_state(self, days_back: int = 7) -> dict:
        """Read-only feed for the Work Item overview panel (Task 6): per work
        item id, whether a Hermes prompt approval is pending (and which
        pending_reason(s)) plus the most recent task_completed /
        permission_requested / process_exited outcome for its linked
        session(s). Returns ``{wi_id: {"pending_reasons": [...], "events":
        {event_type: {"ts", "status", "tool_name"}}}}``, omitting any work
        item with neither.

        Deliberately does NOT call list_pending_agent_decisions(): that method
        issues/rotates one-time operator capabilities as a side effect
        (_issue_operator_capability), and this panel polls independently of
        agent-approvals.js's own poll -- calling it here would silently
        invalidate a capability an approval card is currently holding. Instead
        this reads the same raw layers and joins with
        work_items.resolve_work_item_id (the same join hermes_events'
        publisher and agent_gateway's GET /v1/events already use).

        Never raises: this panel must never show an error state just because
        Hermes or the approval subsystem is not running or nothing is linked.
        """
        try:
            store = work_items.load_store()
        except Exception:
            store = {"items": [], "session_links": {}}
        find_session = getattr(self.pty_manager, "find_by_session_key", lambda _key: None)

        pending_by_wi: dict[str, set] = {}
        try:
            session_links = store.get("session_links") or {}
            for state in agent_decisions.list_pending_decisions():
                target = ((state.get("proposal") or {}).get("action") or {}).get("target") or {}
                session_key = target.get("session_key")
                csid = None
                if session_key:
                    try:
                        session = find_session(session_key)
                    except Exception:
                        session = None
                    csid = getattr(session, "claude_session_id", None) if session else None
                wi_id = work_items.resolve_work_item_id(session_links, session_key, csid)
                if wi_id:
                    pending_by_wi.setdefault(wi_id, set()).add(state.get("pending_reason"))
        except Exception:
            pending_by_wi = {}

        events_by_wi: dict[str, dict] = {}
        try:
            session_links = store.get("session_links") or {}
            since = time.time() - days_back * 86400
            # Widen the read for disambiguation context only, mirroring
            # agent_gateway._handle_events: a Notification landing right at the
            # window edge needs its preceding same-session raw event to tell
            # permission_requested apart from a logged-only status change.
            lookback = max(0.0, since - hermes_events.OVERLAP_WINDOW_S)
            prev_event_by_session: dict[str, str] = {}
            for rec in work_items.read_events(since=lookback):
                session_key = rec.get("session_key")
                prev_event = prev_event_by_session.get(session_key) if session_key else None
                try:
                    normalized = hermes_events.normalize_event(rec, prev_event)
                except Exception:
                    normalized = None
                if session_key:
                    prev_event_by_session[session_key] = rec.get("event")
                if normalized is None:
                    continue
                wi_id = work_items.resolve_work_item_id(
                    session_links, session_key, rec.get("claude_session_id"))
                if not wi_id:
                    continue
                ts = rec.get("ts") or 0
                slot = events_by_wi.setdefault(wi_id, {})
                event_type = normalized["event_type"]
                prior = slot.get(event_type)
                if prior is None or ts > prior["ts"]:
                    slot[event_type] = {
                        "ts": ts,
                        "status": normalized.get("status") or "",
                        "tool_name": normalized.get("tool_name") or "",
                    }
        except Exception:
            events_by_wi = {}

        wi_ids = set(pending_by_wi) | set(events_by_wi)
        return {
            wi_id: {
                "pending_reasons": sorted(r for r in pending_by_wi.get(wi_id, ()) if r),
                "events": events_by_wi.get(wi_id, {}),
            }
            for wi_id in wi_ids
        }

    # ---------- Jira (on-demand, read + transition; token in secrets.json) ----------
    # Every method degrades to a benign empty/false when Jira is disabled — the UI
    # checks jira_available() to decide whether to show the Jira segment at all.
    def jira_available(self) -> bool:
        return jira_client.available()

    def jira_list_issues(self, max_results: int = 50) -> list:
        try:
            return jira_client.list_my_open_issues(max_results)
        except Exception as e:
            print(f"[jira] list failed: {e}")
            return []

    def jira_search(self, text: str, max_results: int = 25) -> list:
        try:
            return jira_client.search_issues(text, max_results)
        except Exception as e:
            print(f"[jira] search failed: {e}")
            return []

    def jira_transitions(self, key: str) -> list:
        try:
            return jira_client.get_transitions(key)
        except Exception as e:
            print(f"[jira] transitions failed: {e}")
            return []

    def jira_transition(self, key: str, transition_id: str) -> bool:
        try:
            return jira_client.transition_issue(key, transition_id)
        except Exception as e:
            print(f"[jira] transition failed: {e}")
            return False

    def refresh_jira_item(self, wi_id: str, key: str) -> dict | None:
        """Refetch one issue and diff it into the work item (status/duedate history).
        Returns the fresh normalized issue, or None if disabled/not found."""
        try:
            fresh = jira_client.fetch_issue(key)
            if fresh is not None:
                work_items.apply_jira_snapshot(wi_id, fresh.get("status"), fresh.get("duedate"))
            return fresh
        except Exception as e:
            print(f"[jira] refresh failed: {e}")
            return None

    # ---------- Teams (on-demand; MSAL cache + graph ids in secrets.json) ----------
    def teams_available(self) -> bool:
        return teams_graph.available()

    def teams_recent(self, top: int = 30, force: bool = False) -> list:
        try:
            return teams_graph.list_recent(top, force=force)
        except Exception as e:
            print(f"[teams] recent failed: {e}")
            return []

    def teams_search(self, query: str, top: int = 25) -> list:
        try:
            return teams_graph.search_messages(query, top)
        except Exception as e:
            print(f"[teams] search failed: {e}")
            return []
