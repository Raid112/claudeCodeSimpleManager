"""
UI diagnostics log — %LOCALAPPDATA%\\ClaudeManager\\logs\\ui-YYYY-MM-DD.jsonl.

Why a file instead of the DevTools console: the console only exists while someone has it
open, and the bug it has to explain (a terminal whose layout breaks — wrong columns, input
that goes nowhere — and only recovers after switching tabs or minimizing the window) starts
minutes before anyone looks. DevTools is also no longer opened on every launch (see
main.py), so this file IS the record.

Both sides append here — the webview through Bridge.ui_log (batched) and the Python side
for PTY resizes — so one file correlates the two clocks: what the frontend thought the
size was vs what the PTY was actually told.

One JSON object per line: {ts, iso, src, event, ...fields}. Best-effort by contract: a
logging failure must never take a terminal down, so every public function swallows its
errors. Only this process writes (hooks do not), so a plain append is safe here.
Files older than KEEP_DAYS are dropped by gc().
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from terminal.hook_state import get_data_dir

KEEP_DAYS = 7
MAX_RECORDS = 200        # per ui_log() call — a runaway frontend loop can't flood the disk
MAX_FIELD_CHARS = 500    # truncate long strings (stack traces) instead of dropping the line


def log_dir() -> Path:
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path(ts: float) -> Path:
    # LOCAL day, same convention as work_items' event partitions: "what happened yesterday"
    # has to mean the user's yesterday.
    return log_dir() / f"ui-{datetime.fromtimestamp(ts).strftime('%Y-%m-%d')}.jsonl"


def _clean(value):
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k)[:60]: _clean(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in list(value)[:20]]
    return str(value)[:MAX_FIELD_CHARS]


def append(records: list, src: str = "ui") -> int:
    """Append already-shaped records (dicts). Returns how many lines were written.

    Each record keeps its own `ts` when it carries one (the frontend's clock, so a batch
    flushed 2s later still says when it happened); `iso` is derived from it for reading by
    eye. Anything unserializable is dropped rather than raising."""
    if not records:
        return 0
    written = 0
    now = time.time()
    try:
        path = _log_path(now)
        with open(path, "a", encoding="utf-8") as stream:
            for rec in records[:MAX_RECORDS]:
                if not isinstance(rec, dict):
                    continue
                ts = rec.get("ts")
                ts = float(ts) if isinstance(ts, (int, float)) else now
                line = {"ts": round(ts, 3),
                        "iso": datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3],
                        "src": rec.get("src") or src,
                        "event": str(rec.get("event") or "?")[:80]}
                for key, value in rec.items():
                    if key in ("ts", "src", "event"):
                        continue
                    line[str(key)[:40]] = _clean(value)
                try:
                    stream.write(json.dumps(line, ensure_ascii=False) + "\n")
                except (TypeError, ValueError):
                    continue
                written += 1
    except OSError:
        return written
    return written


def log(event: str, src: str = "py", **fields) -> None:
    """Log one event from the Python side. Never raises."""
    try:
        append([{"event": event, "src": src, "ts": time.time(), **fields}], src=src)
    except Exception:
        pass


def gc(days: int = KEEP_DAYS) -> None:
    """Drop log files older than `days`. Never raises."""
    try:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        for path in log_dir().glob("ui-*.jsonl"):
            if path.stem[3:] < cutoff:
                path.unlink(missing_ok=True)
    except Exception:
        pass
