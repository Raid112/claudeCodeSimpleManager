#!/usr/bin/env python
"""Hermes-side --script filter for the ClaudeManager wake-up webhook route.

This is the REVIEW COPY. The deployed copy lives at HERMES_HOME/scripts/
claudemanager_event_filter.py (path traversal outside that directory is blocked by
the installed webhook_filters.py) -- installing it there is a separate operator step,
see integrations/hermes/README.md. It runs inside Hermes' own Python interpreter, not
this repo's, so it MUST stay dependency-free stdlib and MUST NOT import anything from
this codebase.

Contract (verified against the installed hermes-agent source,
.hermes/plans/20260803-hermes-webhook-contract.md SS7):
  - stdin receives json.dumps(payload) of the webhook body (headers are not included).
  - stdout: JSON replaces the payload before prompt templating, and delivery
    continues. Empty stdout, the literal "[SILENT]", or a nonzero exit ignores the
    whole webhook (no LLM call, no turn).

The declarative route filters in config.yaml already select event_type and the
hermes_initiated==false recursion guard (both belong there so a bug in this script
can't bypass them). This script does only the two things a declarative filter can't:

  1. Dedup by event_id -- a republish after a ClaudeManager crash/restart reuses the
     same event_id (see terminal/hermes_events.event_id_for), so a duplicate must be
     silenced here.
  2. A per-(session_key, event_type) cooldown, so a burst of the same kind of event
     for one session doesn't start a new Hermes turn every time.

On a dedup or cooldown hit: print "[SILENT]". On a state-file error (corrupt,
unreadable, or unwritable): print the payload unchanged and let the event through --
a broken dedup cache must not silence every future wake-up.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

# Deployed location is HERMES_HOME/scripts/<this file>; the dedup state lives one
# directory up, alongside webhook_subscriptions.json.
DEDUP_STATE_FILE = Path(__file__).resolve().parent.parent / "claudemanager_event_dedup.json"

COOLDOWN_SECONDS = 60      # per (session_key, event_type); named, not a magic number
MAX_DEDUP_ENTRIES = 2000   # bounded persistent state (capped size)


def _load_state():
    """Return the state dict, or None on any state-file problem (caller must then
    fail OPEN -- pass the event through -- rather than silently drop every future
    wake-up because one file got corrupted)."""
    try:
        if not DEDUP_STATE_FILE.exists():
            return {"seen_ids": {}, "cooldowns": {}}
        with open(DEDUP_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        data.setdefault("seen_ids", {})
        data.setdefault("cooldowns", {})
        if not isinstance(data["seen_ids"], dict) or not isinstance(data["cooldowns"], dict):
            return None
        return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _save_state(state: dict) -> bool:
    try:
        for key in ("seen_ids", "cooldowns"):
            items = state.get(key, {})
            if len(items) > MAX_DEDUP_ENTRIES:
                ordered = sorted(items.items(), key=lambda kv: kv[1])
                state[key] = dict(ordered[-MAX_DEDUP_ENTRIES:])
        tmp = DEDUP_STATE_FILE.with_name(f"{DEDUP_STATE_FILE.stem}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, DEDUP_STATE_FILE)
        return True
    except OSError:
        return False


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        print(raw)  # not JSON we can reason about; let it through unchanged
        return 0
    if not isinstance(payload, dict):
        print(raw)
        return 0

    event_id = payload.get("event_id")
    session_key = payload.get("session_key")
    event_type = payload.get("event_type")

    state = _load_state()
    if state is None:
        print(json.dumps(payload))
        return 0

    now = time.time()

    if isinstance(event_id, str) and event_id in state["seen_ids"]:
        print("[SILENT]")
        return 0

    cooldown_key = f"{session_key}|{event_type}"
    last = state["cooldowns"].get(cooldown_key)
    if isinstance(last, (int, float)) and (now - last) < COOLDOWN_SECONDS:
        print("[SILENT]")
        return 0

    if isinstance(event_id, str):
        state["seen_ids"][event_id] = now
    state["cooldowns"][cooldown_key] = now
    _save_state(state)  # best-effort: even if this fails, still deliver this event

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
