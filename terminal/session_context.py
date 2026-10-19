"""Bounded, read-only context extraction for managed Claude sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ABS_WIN = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>]{1,240}")
_ABS_UNIX = re.compile(r"/(?:home|Users|tmp|var|workspace|workspaces)/[^\s\"'<>]{1,240}")
_SECRET = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"
)
_FILE_REF = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|md|json|yaml|yml|toml|rs|go|cs|java|sql|html|css))(?![A-Za-z0-9_])")


def _safe_text(value: str, limit: int = 6000) -> str:
    value = str(value).replace("\x00", " ")
    value = _ABS_WIN.sub("<path>", value)
    value = _ABS_UNIX.sub("<path>", value)
    value = _SECRET.sub(lambda m: f"{m.group(1)}[REDACTED]", value)
    return value[:limit]


def _content_text(content) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return _safe_text(content), []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    refs: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            text = _safe_text(block["text"])
            parts.append(text)
            refs.extend(_FILE_REF.findall(text))
        elif kind == "tool_use":
            name = block.get("name") or "unknown"
            parts.append(f"[tool_use:{_safe_text(str(name), 80)}]")
        elif kind == "tool_result":
            parts.append("[tool_result]")
    return "\n".join(parts), refs


def _normalize_entry(entry: dict) -> tuple[dict | None, list[str]]:
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    role = message.get("role") if isinstance(message, dict) else None
    if role not in {"user", "assistant"}:
        return None, []
    text, refs = _content_text(message.get("content"))
    if not text:
        return None, refs
    item = {
        "role": role,
        "text": text,
        "timestamp": entry.get("timestamp") or message.get("timestamp"),
    }
    return item, refs


def _session_file(provider_session_id: str) -> Path | None:
    if not isinstance(provider_session_id, str) or not _SAFE_ID.fullmatch(provider_session_id):
        return None
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return None
    direct = list(root.glob(f"*/{provider_session_id}.jsonl"))
    for candidate in direct:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    # Some Claude versions nest project metadata one level deeper. Keep this
    # bounded and never accept a caller-provided path.
    count = 0
    for candidate in root.rglob(f"{provider_session_id}.jsonl"):
        count += 1
        if count > 20:
            break
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def read_recent_context(provider_session_id: str, *, limit_turns: int = 12,
                        max_bytes: int = 65536) -> dict:
    """Read a bounded semantic tail of one Claude JSONL session."""
    observed = time.time()
    path = _session_file(provider_session_id)
    base = {
        "source": "claude_jsonl",
        "trust": "external_data",
        "observed_at": observed,
        "available": False,
        "messages": [],
        "files_cited": [],
        "truncated": False,
        "cursor": None,
    }
    if path is None:
        base["error"] = "session_jsonl_not_found"
        return base
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            if stat.st_size > max_bytes:
                handle.seek(-max_bytes, os.SEEK_END)
                handle.readline()  # discard a partial first line
                base["truncated"] = True
            raw = handle.read(max_bytes)
        lines = raw.splitlines()
    except OSError:
        base["error"] = "session_jsonl_unreadable"
        return base

    messages: list[dict] = []
    refs: list[str] = []
    malformed = 0
    for line in lines:
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(entry, dict):
            continue
        item, found_refs = _normalize_entry(entry)
        if item:
            messages.append(item)
            refs.extend(found_refs)
    # A turn is represented by a user/assistant pair in the transcript. Keep a
    # little extra room for tool-only records while bounding response size.
    messages = messages[-max(2, int(limit_turns) * 2):]
    for item in messages:
        item["text"] = item["text"][:6000]
    refs = list(dict.fromkeys(refs))[:100]
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    age = max(0.0, observed - stat.st_mtime)
    base.update({
        "available": True,
        "messages": messages,
        "files_cited": refs,
        "content_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "cursor": str(stat.st_size),
        "age_s": age,
        "freshness": "fresh" if age <= 300 else "stale",
        "last_prompt": next((item["text"] for item in reversed(messages) if item["role"] == "user"), None),
    })
    if malformed:
        base["malformed_lines"] = malformed
    return base
