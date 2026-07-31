"""
pywebview JS API bridge — exposes Python functions to JavaScript.
"""

import json
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

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
SESSIONS_PATH = Path(__file__).parent.parent / "sessions.json"


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

    def __init__(self, pty_manager: PtyManager, ws_server: WebSocketServer):
        self.pty_manager = pty_manager
        self.ws_server = ws_server
        self._window: webview.Window | None = None
        self._toaster = None  # lazily created WindowsToaster (windows_toasts)
        self._operator_capabilities: dict[str, dict] = {}
        self._emergency_capability = secrets.token_urlsafe(32)

    def set_window(self, window: webview.Window):
        self._window = window

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

    def _focus_tab(self, session_id: str):
        """Bring the window to the foreground and select the given tab.

        Runs on the windows_toasts activation thread. The on_top toggle is the
        standard pywebview trick to force the window to the foreground on Windows.
        """
        win = self._window
        if win is None:
            return
        try:
            win.restore()
            win.on_top = True
            win.on_top = False
            win.evaluate_js(f"window.app && window.app.switchToTerminal({json.dumps(session_id)})")
        except Exception as e:
            print(f"_focus_tab failed: {e}")

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

    def close_terminal(self, session_id: str):
        self.ws_server.revoke_session(session_id)
        self.pty_manager.close_session(session_id)

    def get_terminals(self) -> list[dict]:
        return self.pty_manager.get_all_sessions()

    def get_ws_port(self) -> int:
        return self.ws_server.actual_port

    # ---------- Hermes proposal review (UI-only authority) ----------
    def _issue_operator_capability(self, decision_id: str, proposal_hash: str) -> str:
        token = secrets.token_urlsafe(32)
        self._operator_capabilities[token] = {
            "decision_id": decision_id,
            "proposal_hash": proposal_hash,
            "expires_at": time.time() + 120,
            "used": False,
        }
        return token

    def _consume_operator_capability(self, token: str, decision_id: str, proposal_hash: str) -> bool:
        record = self._operator_capabilities.get(token)
        if not record or record["used"] or record["expires_at"] <= time.time():
            return False
        if record["decision_id"] != decision_id or record["proposal_hash"] != proposal_hash:
            return False
        record["used"] = True
        return True

    def list_pending_agent_decisions(self) -> list[dict]:
        pending = agent_decisions.list_pending_decisions()
        for item in pending:
            item["operator_capability"] = self._issue_operator_capability(
                item["decision_id"], item["proposal_hash"])
        return pending

    def approve_agent_proposal(self, decision_id: str, proposal_hash: str,
                               reviewer_id: str, capability: str) -> dict:
        if not self._consume_operator_capability(capability, decision_id, proposal_hash):
            raise PermissionError("invalid or expired operator capability")
        return agent_decisions.approve(decision_id, proposal_hash, reviewer_id, capability)

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

    def archive_work_item(self, wi_id: str, archived: bool = True):
        """Archive/unarchive a whole item; cascades to its sessions (see set_item_archived)."""
        work_items.set_item_archived(wi_id, archived)

    def reorder_work_items(self, ordered_ids: list):
        work_items.reorder(ordered_ids)

    def link_session(self, session_key: str, wi_id: str,
                     group_name: str = None, path: str = None, name: str = None):
        work_items.link(session_key, wi_id, group_name=group_name, path=path, name=name)

    def unlink_session(self, session_key: str):
        work_items.unlink(session_key)

    def archive_session(self, session_key: str, archived: bool = True):
        work_items.set_archived(session_key, archived)

    def work_daily_digest(self, days: int = 2) -> dict:
        """Operational recap: today/yesterday plus active waiting items."""
        return work_items.work_overview(days)

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
