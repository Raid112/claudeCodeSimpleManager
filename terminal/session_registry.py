"""Authoritative public session-key registry and per-session locks."""

from __future__ import annotations

import re
import threading


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _validate(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is not a valid identifier")
    return value


class SessionRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, object] = {}
        self._aliases: dict[str, str] = {}
        self._session_locks: dict[str, threading.RLock] = {}
        self._closing: set[str] = set()

    def register(self, session) -> None:
        key = _validate(session.session_key, "session_key")
        aliases = {
            key,
            _validate(session.id, "manager_key"),
        }
        for value in (getattr(session, "agent_session_id", None),
                      getattr(session, "claude_session_id", None)):
            if value:
                aliases.add(_validate(value, "provider_key"))
        with self._lock:
            for alias in aliases:
                existing = self._aliases.get(alias)
                if existing and existing != key:
                    raise ValueError("session identifier is already registered")
            self._sessions[key] = session
            self._session_locks.setdefault(key, threading.RLock())
            for alias in aliases:
                self._aliases[alias] = key

    def find_by_session_key(self, session_key: str):
        try:
            key = _validate(session_key, "session_key")
        except ValueError:
            return None
        with self._lock:
            return None if key in self._closing else self._sessions.get(key)

    def refresh_aliases(self, session) -> None:
        key = _validate(session.session_key, "session_key")
        with self._lock:
            if key not in self._sessions:
                return
            for value in (getattr(session, "agent_session_id", None),
                          getattr(session, "claude_session_id", None)):
                if value:
                    alias = _validate(value, "provider_key")
                    existing = self._aliases.get(alias)
                    if existing and existing != key:
                        raise ValueError("provider identifier is already registered")
                    self._aliases[alias] = key

    def find_alias(self, alias: str):
        try:
            alias = _validate(alias, "alias")
        except ValueError:
            return None
        with self._lock:
            key = self._aliases.get(alias)
            return None if not key or key in self._closing else self._sessions.get(key)

    def lock_for(self, session_key: str) -> threading.RLock:
        key = _validate(session_key, "session_key")
        with self._lock:
            if key not in self._sessions:
                raise KeyError(session_key)
            return self._session_locks[key]

    def begin_close(self, session_key: str) -> bool:
        key = _validate(session_key, "session_key")
        with self._lock:
            if key not in self._sessions or key in self._closing:
                return False
            self._closing.add(key)
            return True

    def finish_close(self, session_key: str) -> None:
        key = _validate(session_key, "session_key")
        with self._lock:
            self._sessions.pop(key, None)
            self._closing.discard(key)
            for alias, mapped in list(self._aliases.items()):
                if mapped == key:
                    self._aliases.pop(alias, None)
            self._session_locks.pop(key, None)

    def snapshots(self) -> list[dict]:
        with self._lock:
            result = []
            for key, session in self._sessions.items():
                if key in self._closing:
                    continue
                result.append({
                    "session_key": key,
                    "manager_key": session.id,
                    "provider_key": getattr(session, "agent_session_id", None),
                    "legacy_claude_id": getattr(session, "claude_session_id", None),
                    "terminal_type": getattr(session, "terminal_type", None),
                    "closing": False,
                    "closed": False,
                })
            return result
