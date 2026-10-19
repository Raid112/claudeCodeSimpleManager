"""
Authoritative per-session state, written by Claude Code hooks and read by the UI.

Each managed claude session spawns with `--settings <generated hooks file>` whose
hooks re-invoke this app with `--hook-notify` (see main.py). That branch calls
`write_event` here, which derives a UI status from the hook event and persists it
atomically to a per-session JSON file. `PtyManager.get_all_sessions` reads it back.

Design notes:
- Files are keyed by the claude session_id (the uuid we pass via --session-id),
  which the hook payload echoes verbatim.
- Writes are atomic (temp + os.replace) so a concurrent reader never sees a
  half-written file.
- Notification is ambiguous (permission prompt vs idle-waiting); we disambiguate
  by the PREVIOUS event without parsing the English message text.
"""

import os
import json
import time
import uuid
import tempfile
from pathlib import Path


def get_data_dir() -> Path:
    """Writable app data dir. %LOCALAPPDATA%\\ClaudeManager, fallback ~/.claudemanager."""
    base = os.environ.get("LOCALAPPDATA")
    d = (Path(base) / "ClaudeManager") if base else (Path.home() / ".claudemanager")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_dir() -> Path:
    d = get_data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(session_id: str) -> Path:
    return _state_dir() / f"{session_id}.json"


def _derive_status(event_name: str, prev_event: str | None) -> str | None:
    """Map a hook event (+ previous event) to a UI status.

    Returns None for events we don't track (keep prior state untouched).
    """
    if event_name in ("Stop", "StopFailure"):
        return "ready"
    if event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "running"
    if event_name == "PermissionAsk":
        # Mod path only (terminal/mod_bridge.py): tool.check answered `ask`, so the
        # permission dialog is open. Exact, unlike the Notification guess below.
        return "tooluse"
    if event_name == "Notification":
        # Permission prompt arrives right after PreToolUse (no PostToolUse/Stop yet).
        # Idle-waiting arrives after Stop. Default to 'waiting' (safe: never blocks composer).
        if prev_event == "PreToolUse":
            return "tooluse"
        return "waiting"
    return None


def _atomic_write(path: Path, text: str) -> None:
    # Unique temp name in the same dir so os.replace is atomic on Windows even
    # with parallel tool hooks (e.g. concurrent PreToolUse) racing.
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _safe_log_event(session_id: str, event_name: str, tool_name: str | None, ts: float,
                    session_key: str | None = None) -> None:
    """Append the raw hook event to the work-items event log. Best-effort: a lazy
    import + broad catch keep hook_state independent of work_items and unbreakable
    (this runs inside a hook process; a failure here must never abort the hook —
    everything below, including the fallback key computation, stays inside the
    try so nothing here can raise before the broad except catches it).

    The event log is keyed by `session_key` (the stable, tab-lifetime manager
    identity), not by the provider `session_id` — which the user can swap under
    the same tab via /clear or /resume — so a partition and its records survive
    that swap with no migration needed. `session_id` still rides along as
    `claude_session_id` for consumers that need the current provider id.

    Unmanaged sessions (no CLAUDEMANAGER_SESSION_KEY injected, e.g. a claude
    session started outside this app's PTY spawn) have no session_key: fall back
    to keying the log by the provider session_id itself, mirroring write_event's
    own `tab_id or session_id` fallback for the state file, so an unmanaged
    session still logs something instead of silently dropping the event."""
    try:
        from terminal import work_items
        key = session_key or session_id
        work_items.log_event(key, event_name, tool_name=tool_name, ts=ts,
                             claude_session_id=session_id)
    except Exception:
        pass


def write_event(session_id: str, event_name: str, ts: float | None = None,
                tool_name: str | None = None, tab_id: str | None = None,
                session_key: str | None = None, flags: dict | None = None,
                apply_state: bool = True) -> None:
    """Persist derived status for a hook event. Must never raise (callers are hooks).

    State is keyed by `tab_id` (the managed tab, stable while the terminal lives)
    when the hook knows it — NOT by session_id, which changes under the same tab on
    /clear or /resume. The current claude session_id rides along as a field so the
    backend can follow the switch (see PtyManager.get_all_sessions). Falls back to
    session_id keying for unmanaged sessions (no CLAUDEMANAGER_TAB env). `session_key`
    is the stable manager identity (CLAUDEMANAGER_SESSION_KEY); recorded as a field
    so downstream consumers don't need to map tab_id -> session_key at read time.

    `flags` (mod path: `aborted`, `error`) ride along in the state file. With
    `apply_state=False` the event is only logged: the mod bridge passes that for an
    event older than one already applied, so a late delivery cannot move status back."""
    if not session_id or not event_name:
        return
    if ts is None:
        ts = time.time()
    _safe_log_event(session_id, event_name, tool_name, ts, session_key)
    if not apply_state:
        return
    key = tab_id or session_id
    prev = read_state(key)
    prev_event = prev.get("event") if prev else None
    status = _derive_status(event_name, prev_event)
    if status is None:
        return
    data = {
        "event": event_name, "status": status, "ts": ts,
        "session_id": session_id, "session_key": session_key,
    }
    for name, value in (flags or {}).items():
        if value:
            data[name] = True
    _atomic_write(state_path(key), json.dumps(data))


def read_state(session_id: str) -> dict | None:
    """Return the latest state dict, or None. Swallows the temp->replace race window."""
    if not session_id:
        return None
    try:
        with open(state_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def delete_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
    except (FileNotFoundError, OSError):
        pass


def gc_orphans(live_ids: set[str] | None = None, max_age_s: float = 24 * 3600) -> None:
    """Clean up hook-state files.

    - Always removes leftover .tmp files and state files older than `max_age_s`.
    - If `live_ids` is given, also removes state for any session not in it.
      At startup pass None (age-only): restored sessions reuse the same
      session_id and their last-known state is still meaningful.
    """
    try:
        now = time.time()
        for item in _state_dir().iterdir():
            if item.suffix == ".tmp":
                item.unlink(missing_ok=True)
                continue
            if item.suffix != ".json":
                continue
            try:
                stale = (now - item.stat().st_mtime) > max_age_s
            except OSError:
                stale = False
            not_live = live_ids is not None and item.stem not in live_ids
            if stale or not_live:
                item.unlink(missing_ok=True)
    except (FileNotFoundError, OSError):
        pass
