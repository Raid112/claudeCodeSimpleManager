"""
WorkItems data layer — snapshot + two logs. Pure stdlib, no webview dependency.

Three files under get_data_dir() (%LOCALAPPDATA%\\ClaudeManager), mirroring the
style of hook_state.py (module functions, atomic temp+os.replace writes):

- work_items.json  : snapshot {version, items[], session_links{}}. Rewritten on change. Never expires.
- work_log.jsonl   : append, one line per work-item action. Low volume. Never expires.
- events/events-YYYY-MM-DD-<sid>.jsonl : append, one line per hook event. One file PER SESSION per
  day (Windows append is NOT atomic across processes — each hook is a separate main.py process, so a
  shared file loses lines). Per-session partitioning removes contention BETWEEN sessions; it does not
  fix contention WITHIN one session if Claude fires parallel tool-call hooks (concurrent PreToolUse
  processes append to the same file → burst line loss). Accepted: later audit tolerates a dropped
  line in a burst; no cross-process lock for a best-effort metadata log. Retained 60 days.

The session<->work-item link and the per-session `archived` flag live in session_links here, NOT in
sessions.json — that file is rewritten wholesale by the frontend every 10s and would clobber them.
The join key everywhere is the claude_session_id.
"""

import os
import json
import time
import uuid
import glob
from pathlib import Path
from datetime import datetime, timedelta

from terminal.hook_state import get_data_dir

SCHEMA_VERSION = 1
_SORT_STEP = 1000.0


# ---------- paths ----------
def _store_path() -> Path:
    return get_data_dir() / "work_items.json"


def _work_log_path() -> Path:
    return get_data_dir() / "work_log.jsonl"


def _events_dir() -> Path:
    d = get_data_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _event_file(session_id: str, ts: float) -> Path:
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # LOCAL time (daily reasons in local days)
    return _events_dir() / f"events-{day}-{session_id}.jsonl"


# ---------- atomic write (same pattern as hook_state._atomic_write) ----------
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _empty_store() -> dict:
    return {"version": SCHEMA_VERSION, "items": [], "session_links": {}}


# ---------- snapshot ----------
def load_store() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_store()
    # forward-compat backfill
    data.setdefault("version", SCHEMA_VERSION)
    data.setdefault("items", [])
    data.setdefault("session_links", {})
    for it in data["items"]:
        it.setdefault("archived", False)  # forward-compat: items created before archiving
        it.setdefault("workflow_state", "active")
        it.setdefault("waiting_since", None)
    return data


def save_store(store: dict) -> None:
    _atomic_write(_store_path(), json.dumps(store, ensure_ascii=False, indent=2))


def list_items() -> list[dict]:
    return load_store()["items"]


def _find(store: dict, wi_id: str) -> dict | None:
    return next((it for it in store["items"] if it["id"] == wi_id), None)


def new_item(source: str, title: str, external_key: str | None = None,
             external_url: str | None = None, status: str | None = None,
             duedate: str | None = None, duedate_has_time: bool = False,
             person: str | None = None, ts: float | None = None) -> dict:
    """Create a work item. For source=jira, seeds jira_last_seen so the first
    refresh never reports a false status/duedate change."""
    if ts is None:
        ts = time.time()
    store = load_store()
    if external_key:
        existing = next((
            it for it in store["items"]
            if not it.get("merged_into")
            and it.get("source") == source
            and it.get("external_key") == external_key
        ), None)
        if existing is not None:
            return existing
    max_order = max((it.get("sort_order", 0) for it in store["items"]), default=0.0)
    item = {
        "id": "wi_" + uuid.uuid4().hex[:8],
        "source": source,
        "external_key": external_key,
        "external_url": external_url,
        "title": title,
        "status": status,
        "duedate": duedate,
        "duedate_has_time": duedate_has_time,
        "person": person,
        "sort_order": max_order + _SORT_STEP,
        "done": False,
        "archived": False,
        "workflow_state": "active",
        "waiting_since": None,
        "created_at": ts,
        "closed_at": None,
        "jira_last_seen": (
            {"status": status, "duedate": duedate, "ts": ts} if source == "jira" else None
        ),
    }
    store["items"].append(item)
    save_store(store)
    log_action("create", wi=item["id"], detail={"source": source, "key": external_key})
    return item


def rename_item(wi_id: str, title: str, ts: float | None = None) -> dict | None:
    """Rename a work item's title. Returns the updated item (or None if not found)."""
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it:
        return None
    old = it.get("title")
    title = (title or "").strip()
    if not title or title == old:
        return it
    it["title"] = title
    save_store(store)
    log_action("rename", wi=wi_id, detail={"from": old, "to": title})
    return it


def complete_item(wi_id: str, ts: float | None = None) -> None:
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it:
        return
    it["done"] = True
    it["closed_at"] = ts
    save_store(store)
    log_action("complete", wi=wi_id)


def reopen_item(wi_id: str, ts: float | None = None) -> dict | None:
    """Return a completed or archived item to the active queue."""
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it:
        return None
    it["done"] = False
    it["closed_at"] = None
    it["archived"] = False
    it["workflow_state"] = "active"
    it["waiting_since"] = None
    for link in store["session_links"].values():
        if link.get("wi_id") == wi_id:
            link["archived"] = False
            link.pop("archived_before_waiting", None)
    save_store(store)
    log_action("reopen", wi=wi_id, ts=ts)
    return it


def set_waiting(wi_id: str, waiting: bool = True, ts: float | None = None) -> dict | None:
    """Park/unpark an item while keeping its linked PTY sessions alive."""
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it or it.get("done") or it.get("archived"):
        return it
    was_waiting = it.get("workflow_state") == "waiting"
    it["workflow_state"] = "waiting" if waiting else "active"
    if waiting:
        if not was_waiting or not it.get("waiting_since"):
            it["waiting_since"] = ts
    else:
        it["waiting_since"] = None
    for link in store["session_links"].values():
        if link.get("wi_id") != wi_id:
            continue
        if waiting:
            link.setdefault("archived_before_waiting", bool(link.get("archived")))
            link["archived"] = True
        else:
            link["archived"] = bool(link.pop("archived_before_waiting", False))
    save_store(store)
    if was_waiting != waiting:
        log_action("wait" if waiting else "resume", wi=wi_id, ts=ts)
    return it


def set_item_archived(wi_id: str, archived: bool = True, ts: float | None = None) -> None:
    """Archive/unarchive a WHOLE work item and cascade the flag to its linked sessions, so the
    item's tabs vanish from the tab bar + sidebar together (not orphaned to Avulso). Reversible.
    Distinct from complete_item (done): archiving hides without marking the work finished."""
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it:
        return
    it["archived"] = archived
    for link in store["session_links"].values():
        if link.get("wi_id") == wi_id:
            link["archived"] = archived or it.get("workflow_state") == "waiting"
    save_store(store)
    log_action("archive_item" if archived else "unarchive_item", wi=wi_id)


def reorder(ordered_ids: list[str]) -> None:
    """Rewrite sort_order to match the given order. NOT logged (view preference, not a work fact)."""
    store = load_store()
    pos = {wi_id: i for i, wi_id in enumerate(ordered_ids)}
    for it in store["items"]:
        if it["id"] in pos:
            it["sort_order"] = (pos[it["id"]] + 1) * _SORT_STEP
    save_store(store)


def apply_jira_snapshot(wi_id: str, status: str | None, duedate: str | None,
                        ts: float | None = None) -> None:
    """Diff a freshly fetched Jira status/duedate against jira_last_seen; log status/duedate
    changes and update. Seeded last_seen (from new_item) means the first refresh is a no-op."""
    if ts is None:
        ts = time.time()
    store = load_store()
    it = _find(store, wi_id)
    if not it:
        return
    seen = it.get("jira_last_seen") or {}
    if seen.get("status") != status:
        log_action("status", wi=wi_id, detail={"from": seen.get("status"), "to": status})
    if seen.get("duedate") != duedate:
        log_action("duedate", wi=wi_id, detail={"from": seen.get("duedate"), "to": duedate})
    it["status"] = status
    it["duedate"] = duedate
    it["jira_last_seen"] = {"status": status, "duedate": duedate, "ts": ts}
    save_store(store)


# ---------- session <-> item link (durable; NOT in sessions.json) ----------
def link(claude_session_id: str, wi_id: str, group_name: str | None = None,
         path: str | None = None, name: str | None = None) -> None:
    if not claude_session_id or not wi_id:
        return
    store = load_store()
    entry = store["session_links"].get(claude_session_id, {})
    entry.update({"wi_id": wi_id, "ts": time.time()})
    entry.setdefault("archived", False)
    # Directory hints let "nova aba nesta issue" reopen in the same folder, and let the
    # archived list show a session without a live terminal. Only overwrite when provided.
    if group_name is not None:
        entry["group_name"] = group_name
    if path is not None:
        entry["path"] = path
    if name is not None:
        entry["name"] = name
    store["session_links"][claude_session_id] = entry
    save_store(store)
    log_action("link", wi=wi_id, session=claude_session_id)


def unlink(claude_session_id: str) -> None:
    store = load_store()
    entry = store["session_links"].pop(claude_session_id, None)
    if entry is None:
        return
    save_store(store)
    log_action("unlink", wi=entry.get("wi_id"), session=claude_session_id)


def migrate_link(old_sid: str, new_sid: str) -> None:
    """Re-key a session's link/archived entry when the claude session_id changes
    under the same tab (user ran /clear or /resume). No-op if there's nothing to
    move or the target already has an entry (don't clobber a real link)."""
    if not old_sid or not new_sid or old_sid == new_sid:
        return
    store = load_store()
    links = store["session_links"]
    entry = links.get(old_sid)
    if entry is None or new_sid in links:
        return
    links[new_sid] = entry
    del links[old_sid]
    save_store(store)
    log_action("migrate", wi=entry.get("wi_id"), session=new_sid, detail={"from": old_sid})


def set_archived(claude_session_id: str, archived: bool = True) -> None:
    store = load_store()
    entry = store["session_links"].setdefault(claude_session_id, {"wi_id": None})
    entry["archived"] = archived
    save_store(store)
    log_action("archive" if archived else "unarchive", session=claude_session_id)


# ---------- work log (low volume, never expires) ----------
def log_action(kind: str, wi: str | None = None, session: str | None = None,
               detail: dict | None = None, ts: float | None = None) -> None:
    if ts is None:
        ts = time.time()
    rec = {"ts": ts, "kind": kind}
    if wi is not None:
        rec["wi"] = wi
    if session is not None:
        rec["session"] = session
    if detail is not None:
        rec["detail"] = detail
    try:
        with open(_work_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_work_log(since: float | None = None) -> list[dict]:
    """All work-log records (optionally since a ts), oldest first. Tolerates corrupt lines."""
    p = _work_log_path()
    out: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None and rec.get("ts", 0) < since:
                    continue
                out.append(rec)
    except (FileNotFoundError, OSError):
        return []
    return out


def _day_bounds(days_back: int) -> tuple[float, float, str]:
    """(start_ts, end_ts, label) for a local calendar day `days_back` days ago."""
    today = datetime.now().date()
    day = today - timedelta(days=days_back)
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), day.strftime("%Y-%m-%d")


def daily_digest(days: int = 2) -> list[dict]:
    """Daily-standup recap: for each of the last `days` local days (today first), the work
    items touched, grouped by item. Reorder/archive are view noise; waiting, resuming, and
    reopening are real workflow transitions. Never raises."""
    store = load_store()
    by_id = {it["id"]: it for it in store.get("items", [])}
    KINDS = {"create", "link", "complete", "status", "duedate", "wait", "resume", "reopen"}
    log = read_work_log()
    out = []
    for d in range(days):
        start, end, label = _day_bounds(d)
        recs = [r for r in log if start <= r.get("ts", 0) < end and r.get("kind") in KINDS]
        items: dict[str, dict] = {}
        for r in recs:
            wi = r.get("wi")
            if not wi:
                continue
            it = by_id.get(wi)
            if it and it.get("merged_into"):
                wi = it["merged_into"]
                it = by_id.get(wi)
            slot = items.setdefault(wi, {
                "wi_id": wi,
                "title": (it or {}).get("title") or "(item removido)",
                "external_key": (it or {}).get("external_key"),
                "source": (it or {}).get("source"),
                "kinds": set(),
                "done": bool((it or {}).get("done")),
            })
            slot["kinds"].add(r.get("kind"))
        # serialize sets -> sorted lists
        for slot in items.values():
            slot["kinds"] = sorted(slot["kinds"])
        out.append({"day": label, "days_back": d, "items": list(items.values())})
    return out


# ---------- event log (metadata only; one file per session per day) ----------
def log_event(session_id: str, event_name: str, tool_name: str | None = None,
              ts: float | None = None) -> None:
    """Append one hook event. Metadata only — never tool input (privacy: Q7)."""
    if not session_id or not event_name:
        return
    if ts is None:
        ts = time.time()
    rec = {"ts": ts, "session": session_id, "event": event_name, "tool": tool_name}
    try:
        with open(_event_file(session_id, ts), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(since: float | None = None, until: float | None = None) -> list[dict]:
    """Glob + merge event partitions, tolerating half-written lines (Windows append)."""
    out: list[dict] = []
    for fp in glob.glob(str(_events_dir() / "events-*.jsonl")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # skip corrupt line, keep the good ones
                    t = rec.get("ts", 0)
                    if since is not None and t < since:
                        continue
                    if until is not None and t > until:
                        continue
                    out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def work_overview(days: int = 2) -> dict:
    """Daily's 80/20 overview: recent work plus current waiting items."""
    store = load_store()
    items = store.get("items", [])
    waiting_items = [
        it for it in items
        if not it.get("done")
        and not it.get("archived")
        and it.get("workflow_state") == "waiting"
    ]
    last_by_wi = {
        it["id"]: max(it.get("created_at") or 0, it.get("waiting_since") or 0)
        for it in waiting_items
    }
    waiting_ids = set(last_by_wi)
    session_to_wi = {
        sid: link.get("wi_id")
        for sid, link in store.get("session_links", {}).items()
        if link.get("wi_id") in waiting_ids
    }
    for rec in read_work_log():
        wi_id = rec.get("wi") or session_to_wi.get(rec.get("session"))
        if wi_id in waiting_ids:
            last_by_wi[wi_id] = max(last_by_wi[wi_id], rec.get("ts") or 0)
    since = min((
        it.get("waiting_since") or it.get("created_at") or 0
        for it in waiting_items
    ), default=None)
    if since is not None:
        for rec in read_events(since=since):
            wi_id = session_to_wi.get(rec.get("session"))
            if wi_id in waiting_ids:
                last_by_wi[wi_id] = max(last_by_wi[wi_id], rec.get("ts") or 0)
    waiting = [{
        "wi_id": it["id"],
        "title": it.get("title") or "",
        "external_key": it.get("external_key"),
        "source": it.get("source"),
        "person": it.get("person"),
        "waiting_since": it.get("waiting_since"),
        "last_activity": last_by_wi.get(it["id"], 0),
    } for it in sorted(waiting_items, key=lambda x: x.get("sort_order") or 0)]
    digest = daily_digest(days)
    completed_today = sum(
        1 for it in (digest[0]["items"] if digest else [])
        if "complete" in it.get("kinds", [])
    )
    active_count = sum(
        1 for it in items
        if not it.get("done")
        and not it.get("archived")
        and it.get("workflow_state") != "waiting"
    )
    return {
        "days": digest,
        "waiting": waiting,
        "counts": {
            "active": active_count,
            "waiting": len(waiting),
            "completed_today": completed_today,
        },
    }


def gc_events(max_age_days: int = 60) -> None:
    """Delete event partitions whose day-in-name is older than max_age_days (local time)."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).date()
    for fp in glob.glob(str(_events_dir() / "events-*.jsonl")):
        name = Path(fp).stem  # events-YYYY-MM-DD-<sid>
        parts = name.split("-")
        if len(parts) < 4:
            continue
        try:
            day = datetime.strptime("-".join(parts[1:4]), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(fp)
            except OSError:
                pass
