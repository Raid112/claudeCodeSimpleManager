"""
pywebview JS API bridge — exposes Python functions to JavaScript.
"""

import json
import webbrowser
import webview
from pathlib import Path
from terminal.pty_manager import PtyManager
from terminal.ws_server import WebSocketServer

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
SESSIONS_PATH = Path(__file__).parent.parent / "sessions.json"


DEFAULT_LAYOUT = {"split_ratio": 0.6, "composer_collapsed": False}


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
        )
        return {
            "session_id": session.id,
            "ws_port": self.ws_server.actual_port,
            "group_name": group_name,
            "path": path,
            "terminal_type": terminal_type,
        }

    def close_terminal(self, session_id: str):
        self.pty_manager.close_session(session_id)

    def get_terminals(self) -> list[dict]:
        return self.pty_manager.get_all_sessions()

    def get_ws_port(self) -> int:
        return self.ws_server.actual_port

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
                    "terminal_type": session.terminal_type,
                }
            )
        _save_sessions({"tabs": tabs, "active_tab_index": 0})

    def open_url(self, url: str):
        webbrowser.open(url)
