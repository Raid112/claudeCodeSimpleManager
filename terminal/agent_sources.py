"""Checkpointed, read-only Jira/Teams context polling."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from terminal.hook_state import get_data_dir


class SourcePoller:
    def __init__(self, max_seen: int = 2000):
        self.max_seen = max_seen
        self._lock = threading.RLock()

    def _root(self) -> Path:
        root = Path(os.environ.get("CLAUDEMANAGER_AGENT_SOURCES_DIR") or
                    (get_data_dir() / "agent-sources"))
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        return root

    def _path(self) -> Path:
        return self._root() / "checkpoints.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            data = {}
        return {
            "jira": list(data.get("jira", []))[-self.max_seen:],
            "teams": list(data.get("teams", []))[-self.max_seen:],
        }

    def _save(self, data: dict) -> None:
        path = self._path()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with temp.open("r+", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    @staticmethod
    def _context(source: str, object_id: str, content: dict) -> dict:
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "source": source,
            "object_id": object_id,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "trust": "external_data",
            "content": copy.deepcopy(content),
        }

    def poll(self, jira_adapter, teams_adapter, *, jira_limit: int = 50,
             teams_limit: int = 30) -> dict:
        with self._lock:
            checkpoint = self._load()
            result = {"contexts": [], "errors": []}
            next_checkpoint = copy.deepcopy(checkpoint)
            try:
                jira_items = jira_adapter.list_my_open_issues(jira_limit)
                seen = set(checkpoint["jira"])
                for item in jira_items:
                    key = item.get("external_key")
                    if not key:
                        continue
                    object_id = f"jira:{key}"
                    if object_id not in seen:
                        result["contexts"].append(self._context("jira", object_id, item))
                    seen.add(object_id)
                next_checkpoint["jira"] = list(seen)[-self.max_seen:]
            except Exception as exc:
                result["errors"].append({"source": "jira", "error": type(exc).__name__})

            try:
                chats = teams_adapter.list_recent(teams_limit)
                seen = set(checkpoint["teams"])
                for chat in chats:
                    chat_id = chat.get("chat_id")
                    for message in chat.get("messages", []):
                        msg_id = message.get("msg_id")
                        if not chat_id or not msg_id:
                            continue
                        object_id = f"teams:{chat_id}:{msg_id}"
                        if object_id not in seen:
                            result["contexts"].append(self._context("teams", object_id, {
                                "chat_id": chat_id,
                                "chat_name": chat.get("chat_name"),
                                "sender_name": message.get("sender_name"),
                                "text": message.get("text", ""),
                                "ts": message.get("ts"),
                            }))
                        seen.add(object_id)
                next_checkpoint["teams"] = list(seen)[-self.max_seen:]
            except Exception as exc:
                result["errors"].append({"source": "teams", "error": type(exc).__name__})

            # A successful source advances only its own checkpoint. A failed source's
            # previous checkpoint remains intact, so recovery replays no data loss.
            if result["errors"]:
                if any(error["source"] == "jira" for error in result["errors"]):
                    next_checkpoint["jira"] = checkpoint["jira"]
                if any(error["source"] == "teams" for error in result["errors"]):
                    next_checkpoint["teams"] = checkpoint["teams"]
            try:
                self._save(next_checkpoint)
            except OSError:
                result["errors"].append({"source": "checkpoint", "error": "storage"})
            return result
