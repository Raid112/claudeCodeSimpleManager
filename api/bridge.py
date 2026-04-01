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


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"groups": []}


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

    def set_window(self, window: webview.Window):
        self._window = window

    def get_groups(self) -> list[dict]:
        config = _load_config()
        return config.get("groups", [])

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

    def open_terminal(self, group_name: str, path: str, cols: int = 120, rows: int = 30,
                      continue_session: bool = False, claude_session_id: str = None) -> dict:
        session = self.pty_manager.create_session(group_name, path, cols, rows,
                                                  continue_session=continue_session,
                                                  claude_session_id=claude_session_id)
        return {
            "session_id": session.id,
            "ws_port": self.ws_server.actual_port,
            "group_name": group_name,
            "path": path,
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
            tabs.append({
                "group_name": session.group_name,
                "path": session.path,
                "tab_order": i,
                "claude_session_id": session.claude_session_id,
            })
        _save_sessions({"tabs": tabs, "active_tab_index": 0})

    def open_url(self, url: str):
        webbrowser.open(url)
