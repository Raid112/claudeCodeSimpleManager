"""Normalizer + publisher for ClaudeManager -> Hermes webhook wake-up events.

Contract source: `.hermes/plans/20260803-hermes-webhook-contract.md` (verified against
the installed hermes-agent source; overrides the older plan doc wherever they differ).

Pipeline: `work_items.read_events()` (raw Claude Code hook events + a synthetic
`PROCESS_EXITED_EVENT` from the PTY-exit producer) -> `normalize_event()` (pure mapping
to the wire event model, or None for logged-only/unrecognized events) -> the publisher
stamps the recursion-guard fields from the approval journal -> HMAC-signed POST to the
Hermes gateway's static webhook route.

`normalize_event` is deliberately pure (no I/O) so `terminal/agent_gateway.py`'s
`GET /v1/events` route (Task 4) reuses the exact same mapping rule instead of
re-deriving it.

Wire event model (never `null` for an optional field -- Hermes' prompt templating
preserves the literal `{placeholder}` for a missing key but renders the literal string
"None" for a null, contract doc SS8):

    event_id                  sha256 of (session_key, raw hook event, ts, tool_name)
    event_type                task_completed | permission_requested | process_exited
    session_key               stable manager identity
    work_item_id              "" from normalize_event itself (pure, no I/O); both
                               consumers (this module's publisher, via
                               _stamp_work_item_id, and agent_gateway's GET
                               /v1/events) resolve it with
                               work_items.resolve_work_item_id before the event
                               leaves this process
    claude_session_id         "" when unknown
    status                    ready | tooluse | exited
    tool_name                 "" when absent
    ts                        ISO-8601 with timezone
    source                    claude_hook | pty_process
    hermes_initiated          bool
    last_hermes_write_age_s   int seconds, or "" when there is no known host write

Only three raw-event cases are published; everything else is logged-only (already on
disk via work_items, nothing further to do):

    Stop                                -> task_completed / ready       (published)
    Notification, prev == PreToolUse    -> permission_requested/tooluse (published)
    PermissionAsk (mod path)            -> permission_requested/tooluse (published)
    StopFailure (mod path)              -> logged only
    Notification, prev == Stop          -> (status_changed/waiting)     logged only
    PROCESS_EXITED_EVENT                -> process_exited / exited      (published)
    UserPromptSubmit/PreToolUse/PostToolUse                             logged only
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from terminal.hook_state import get_data_dir, _derive_status
from terminal import work_items
from terminal import agent_decisions

# Synthetic raw event name for a PTY alive->dead transition. Not a real Claude Code
# hook name (those are Stop/Notification/UserPromptSubmit/Pre|PostToolUse). The PTY-exit
# producer (Task 4, built separately) must call work_items.log_event(session_key,
# PROCESS_EXITED_EVENT, ...) with exactly this string so this mapping (and the later
# GET /v1/events route, which imports this same module) recognizes it.
PROCESS_EXITED_EVENT = "ProcessExited"

DEFAULT_WEBHOOK_URL = "http://127.0.0.1:8644/webhooks/claudemanager-events"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8644/health"

# Recursion-guard threshold: how long after a host prompt write we still consider a
# following hook event "possibly caused by that write" (hermes_initiated=True). A
# threshold, not an operator flag, because a flag could be flipped to mask exactly the
# feedback loop this guard exists to catch. 120s covers PTY write -> Claude Code
# processing -> tool calls -> Stop/Notification hook, for a normal turn.
HERMES_INITIATED_THRESHOLD_S = 120

BACKOFF_INITIAL_S = 2.0
BACKOFF_MAX_S = 60.0

# Multiple hook records can share an identical `ts` (same wall-clock second across
# concurrent processes), so cursor resume needs a bounded overlap window plus a
# recent-id set rather than a bare "since" timestamp, or a restart could duplicate or
# skip a same-ts record depending on read_events' `<` boundary.
OVERLAP_WINDOW_S = 5.0
MAX_RECENT_IDS = 2000
CURSOR_FILE_NAME = "hermes_events_cursor.json"

# Reading the journal takes a cross-process file lock and re-reads the whole hash
# chain (agent_decisions._read_events_unlocked re-verifies every event on every read).
# Doing that on every tick would make each poll cycle O(journal size); cache it briefly
# and only refresh when there is at least one publishable event to stamp.
JOURNAL_CACHE_TTL_S = 5.0


# --------------------------------------------------------------------------------
# event_id + normalization (pure)
# --------------------------------------------------------------------------------
def event_id_for(session_key: str | None, raw_event: str | None, ts: float,
                  tool_name: str | None) -> str:
    """Deterministic id so a republish after a crash yields the same id and Hermes'
    filter script (dedup by event_id) treats it as a duplicate rather than a new
    wake-up. Canonical JSON array (not a delimited string) so a tool_name containing
    a separator character cannot forge a collision with a different session/event."""
    canonical = json.dumps(
        [session_key or "", raw_event or "", f"{float(ts):.6f}", tool_name or ""],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_event(record: dict, prev_event: str | None) -> dict | None:
    """Map one raw event-log record to the wire event model, or None when it is not
    one of the three published types (logged-only, or unrecognized).

    `record` is one item from work_items.read_events(): {"ts", "session_key",
    "event", "tool", "claude_session_id"}. Pure: does no I/O. hermes_initiated and
    last_hermes_write_age_s are left at their safe defaults here; the publisher
    (which has journal access) overwrites them via _stamp_guard_fields before
    sending.
    """
    session_key = record.get("session_key")
    if not session_key:
        # Pre-rekey records (old shape, session_key normalized to None) and
        # unmanaged sessions have no identity Hermes can act on; emitting "" would
        # collapse all of them into one dedup/cooldown bucket on the Hermes side.
        return None
    raw_event = record.get("event")
    ts = record.get("ts")
    if not isinstance(ts, (int, float)):
        return None

    if raw_event == PROCESS_EXITED_EVENT:
        event_type, status, source = "process_exited", "exited", "pty_process"
    elif raw_event == "Stop":
        event_type = "task_completed"
        status = _derive_status(raw_event, prev_event)
        source = "claude_hook"
    elif raw_event == "PermissionAsk" or (raw_event == "Notification" and prev_event == "PreToolUse"):
        event_type = "permission_requested"
        status = _derive_status(raw_event, prev_event)
        source = "claude_hook"
    else:
        # Notification-after-Stop, UserPromptSubmit/Pre/PostToolUse, and anything
        # else unrecognized: already on disk via work_items, nothing more to do.
        return None

    ts = float(ts)
    return {
        "event_id": event_id_for(session_key, raw_event, ts, record.get("tool")),
        "event_type": event_type,
        "session_key": session_key,
        "work_item_id": "",
        "claude_session_id": record.get("claude_session_id") or "",
        "status": status or "",
        "tool_name": record.get("tool") or "",
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "source": source,
        "hermes_initiated": False,
        "last_hermes_write_age_s": "",
    }


def _parse_iso(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------------
# HMAC (contract doc SS1) -- V2 generic format, bare hex, no "sha256=" prefix
# --------------------------------------------------------------------------------
SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"


def sign_v2(secret: str, ts: str, body: bytes) -> str:
    import hmac
    signed_content = ts.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------------
# cursor persistence
# --------------------------------------------------------------------------------
def _cursor_path() -> Path:
    return get_data_dir() / CURSOR_FILE_NAME


def _load_cursor() -> dict:
    try:
        with open(_cursor_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("last_ts"), (int, float)):
            raise ValueError("malformed cursor")
        recent = data.get("recent_ids")
        if not isinstance(recent, dict):
            recent = {}
        return {"last_ts": float(data["last_ts"]), "recent_ids": recent}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        # No cursor (first run) or a corrupt one: start from now. read_events() with
        # since=None would otherwise replay up to 60 days of retained history on a
        # fresh install.
        return {"last_ts": time.time(), "recent_ids": {}}


def _save_cursor(cursor: dict) -> None:
    tmp = _cursor_path().with_name(f"{CURSOR_FILE_NAME}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cursor, f)
    os.replace(tmp, _cursor_path())


# --------------------------------------------------------------------------------
# publisher
# --------------------------------------------------------------------------------
class HermesEventsPublisher:
    """Daemon-thread publisher. Nothing raised here may reach the app, the PTY, or a
    hook: `_run` catches everything around `tick()`, and `tick()` catches errors
    around each individual record so one bad record cannot stall the whole batch.

    `poll_interval_s` is 10s, not 2s like the UI's status poll: each tick scans the
    day's event partitions (~100 ms measured against a 5.5 MB / 44k-record retained
    log, after the partition-day pre-filter in work_items.read_events), so a 2s
    interval burned ~5% of a core continuously for nothing. Wake-up latency is not a
    constraint here — the woken turn is told to re-read live state precisely because
    the event may already be stale.
    """

    def __init__(self, secret: str, *, webhook_url: str = DEFAULT_WEBHOOK_URL,
                 health_url: str = DEFAULT_HEALTH_URL, timeout_s: float = 5.0,
                 poll_interval_s: float = 10.0):
        if not isinstance(secret, str) or not secret:
            raise ValueError("webhook secret is required")
        self._secret = secret
        self._webhook_url = webhook_url
        self._health_url = health_url
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

        self._cursor = _load_cursor()
        self._prev_event_by_session: dict[str, str] = {}

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._disabled = False
        self._disabled_reason: str | None = None
        self._state = "running"
        self._current_backoff_s = 0.0

        self._n_published = 0
        self._n_skipped_bad = 0
        self._n_logged_only = 0
        self._n_backoff_cycles = 0
        self._last_error: str | None = None

        self._journal_cache_loaded_at = 0.0
        self._journal_cache_ok = False
        self._journal_last_write_ts: dict[str, float] = {}

        self._session_links_cache: dict = {}

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="claudemanager-hermes-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self._last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            sleep_s = self._current_backoff_s or self._poll_interval_s
            self._stop_event.wait(sleep_s)

    def status(self) -> dict:
        return {
            "state": self._state,
            "reason": self._disabled_reason,
            "last_error": self._last_error,
            "counters": {
                "published": self._n_published,
                "skipped_bad": self._n_skipped_bad,
                "logged_only": self._n_logged_only,
                "backoff_cycles": self._n_backoff_cycles,
            },
        }

    # ---- one poll cycle ----
    def tick(self) -> None:
        if self._disabled:
            return
        if not self._health_ready():
            self._enter_backoff()
            return  # not ready: back off, consume nothing (cursor untouched)

        since = max(0.0, self._cursor["last_ts"] - OVERLAP_WINDOW_S)
        try:
            records = work_items.read_events(since=since)
        except Exception as exc:
            self._last_error = f"read_events failed: {exc.__class__.__name__}"[:200]
            return

        batch = []
        for rec in records:
            ts = rec.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            event_id = event_id_for(rec.get("session_key"), rec.get("event"), ts, rec.get("tool"))
            session_key = rec.get("session_key")
            prev_event = self._prev_event_by_session.get(session_key) if session_key else None
            already_seen = event_id in self._cursor["recent_ids"]
            normalized = None
            if not already_seen:
                try:
                    normalized = normalize_event(rec, prev_event)
                except Exception as exc:
                    # A bad record must not stall the batch: log and treat as logged-only.
                    self._last_error = f"normalize_event failed: {exc.__class__.__name__}"[:200]
                    normalized = None
            # Track ordering context for every record seen, even a re-read one from
            # the overlap window, or a resumed publisher would mis-disambiguate the
            # next Notification for this session.
            if session_key:
                self._prev_event_by_session[session_key] = rec.get("event")
            if already_seen:
                continue
            batch.append({"ts": float(ts), "event_id": event_id, "normalized": normalized})

        if any(item["normalized"] is not None for item in batch):
            self._refresh_journal_cache()
            self._refresh_session_links_cache()

        try:
            for item in batch:
                normalized = item["normalized"]
                if normalized is None:
                    self._n_logged_only += 1
                    self._advance(item["event_id"], item["ts"])
                    continue
                self._stamp_guard_fields(normalized)
                self._stamp_work_item_id(normalized)
                outcome = self._publish_one(normalized)
                if outcome == "sent":
                    self._n_published += 1
                    self._advance(item["event_id"], item["ts"])
                    self._reset_backoff()
                elif outcome == "skip_bad":
                    self._n_skipped_bad += 1
                    self._advance(item["event_id"], item["ts"])
                elif outcome == "transient":
                    self._enter_backoff()
                    return  # stop the batch; retry this same event next tick
                elif outcome == "disabled":
                    return  # _publish_one already flipped _disabled with a reason
        finally:
            _save_cursor(self._cursor)

    def _advance(self, event_id: str, ts: float) -> None:
        self._cursor["recent_ids"][event_id] = ts
        if ts > self._cursor["last_ts"]:
            self._cursor["last_ts"] = ts
        cutoff = self._cursor["last_ts"] - OVERLAP_WINDOW_S
        recent = {k: v for k, v in self._cursor["recent_ids"].items() if v >= cutoff}
        if len(recent) > MAX_RECENT_IDS:
            recent = dict(sorted(recent.items(), key=lambda kv: kv[1])[-MAX_RECENT_IDS:])
        self._cursor["recent_ids"] = recent

    # ---- readiness ----
    def _health_ready(self) -> bool:
        try:
            req = urllib.request.Request(self._health_url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                if resp.status != 200:
                    return False
                body = json.loads(resp.read().decode("utf-8"))
                return isinstance(body, dict) and body.get("status") == "ok"
        except Exception:
            return False

    # ---- delivery ----
    def _publish_one(self, event: dict) -> str:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))  # sign right before sending (300s server-side replay window)
        signature = sign_v2(self._secret, ts, body)
        req = urllib.request.Request(
            self._webhook_url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signature,
                TIMESTAMP_HEADER: ts,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                if resp.status in (200, 202):
                    return "sent"
                self._last_error = f"unexpected status {resp.status}"
                return "transient"
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                exc.read()
            except Exception:
                pass
            if code in (429, 502):
                self._last_error = f"http {code}"
                return "transient"
            if code in (400, 413):
                self._last_error = f"http {code}"
                return "skip_bad"
            if code in (401, 403, 404):
                reason = {401: "auth_rejected", 403: "route_forbidden", 404: "route_not_found"}[code]
                self._disable(reason)
                return "disabled"
            self._last_error = f"http {code}"
            return "transient"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._last_error = f"connection error: {exc.__class__.__name__}"
            return "transient"

    def _disable(self, reason: str) -> None:
        self._disabled = True
        self._state = "disabled"
        self._disabled_reason = reason

    def _enter_backoff(self) -> None:
        self._state = "backing_off"
        self._n_backoff_cycles += 1
        self._current_backoff_s = min(
            BACKOFF_MAX_S, (self._current_backoff_s * 2) if self._current_backoff_s else BACKOFF_INITIAL_S)

    def _reset_backoff(self) -> None:
        self._current_backoff_s = 0.0
        if self._state != "disabled":
            self._state = "running"

    # ---- work_item_id join (reads work_items.json) ----
    def _refresh_session_links_cache(self) -> None:
        """Load session_links once per tick's batch, not once per event --
        avoids reparsing work_items.json per event when a tick has several
        publishable records. Refreshed on the same gate as the journal cache
        (only when the batch has at least one publishable event), so
        staleness is bounded by poll_interval_s -- the same freshness the
        recursion guard already accepts for the journal."""
        try:
            self._session_links_cache = work_items.load_store().get("session_links", {})
        except Exception as exc:
            self._session_links_cache = {}
            self._last_error = f"session_links read failed: {exc.__class__.__name__}"[:200]

    def _stamp_work_item_id(self, event: dict) -> None:
        event["work_item_id"] = work_items.resolve_work_item_id(
            self._session_links_cache, event.get("session_key"), event.get("claude_session_id"))

    # ---- recursion guard (reads the approval journal) ----
    def _refresh_journal_cache(self) -> None:
        now = time.time()
        if now - self._journal_cache_loaded_at < JOURNAL_CACHE_TTL_S:
            return
        self._journal_cache_loaded_at = now
        try:
            # read_audit_events() is the public, hash-chain-verified accessor
            # (terminal/agent_decisions.py) added for exactly this need --
            # list_pending_decisions() deliberately excludes EXECUTED decisions,
            # which is exactly the completed host prompt write this guard needs
            # to see.
            events = agent_decisions.read_audit_events()
        except Exception as exc:
            # Fail closed on the guard, not on the publisher: a corrupt/unreadable
            # journal must not silence every future wake-up. Declarative filters
            # and the Hermes-side dedup/cooldown script still stand on their own.
            self._journal_cache_ok = False
            self._journal_last_write_ts = {}
            self._last_error = f"journal read failed: {exc.__class__.__name__}"[:200]
            return

        session_key_by_decision: dict[str, str] = {}
        last_write_ts: dict[str, float] = {}
        for ev in events:
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = ev.get("kind")
            if kind == "proposal_created":
                target = (payload.get("action") or {}).get("target") or {}
                session_key = target.get("session_key")
                decision_id = payload.get("decision_id")
                if session_key and decision_id:
                    session_key_by_decision[decision_id] = session_key
            elif kind == "execution_result":
                decision_id = payload.get("decision_id")
                session_key = session_key_by_decision.get(decision_id)
                if not session_key:
                    continue
                write_ts = _parse_iso(ev.get("ts"))
                if write_ts is None:
                    continue
                if session_key not in last_write_ts or write_ts > last_write_ts[session_key]:
                    last_write_ts[session_key] = write_ts
        self._journal_cache_ok = True
        self._journal_last_write_ts = last_write_ts

    def _stamp_guard_fields(self, event: dict) -> None:
        session_key = event.get("session_key")
        write_ts = self._journal_last_write_ts.get(session_key) if self._journal_cache_ok else None
        if write_ts is None:
            event["hermes_initiated"] = False
            event["last_hermes_write_age_s"] = ""
            return
        event_ts = _parse_iso(event.get("ts"))
        if event_ts is None or write_ts > event_ts:
            # A write that happened AFTER this event cannot have caused it (also
            # guards against clock skew producing a negative age).
            event["hermes_initiated"] = False
            event["last_hermes_write_age_s"] = ""
            return
        age = event_ts - write_ts
        event["last_hermes_write_age_s"] = int(age)
        event["hermes_initiated"] = age < HERMES_INITIATED_THRESHOLD_S
