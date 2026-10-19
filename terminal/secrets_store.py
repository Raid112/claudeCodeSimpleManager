"""
Secrets loader — reads %LOCALAPPDATA%\\ClaudeManager\\secrets.json.

Kept OUT of git and OUT of config.json (which is versioned and rewritten at runtime).
Read once, held in memory. Tolerates absence: no file -> Jira/Teams disabled, the app
still runs with manual TODOs only.

Schema:
{
  "jira":  { "email": "...", "token": "<manual API token>" },
  "graph": { "client_id": "...", "tenant_id": "..." },
  "hermes_webhook": { "secret": "<HMAC secret shared with Hermes' WEBHOOK_SECRET>" }
}
"""

import json
from pathlib import Path

from terminal.hook_state import get_data_dir

_cache: dict | None = None


def _secrets_path() -> Path:
    return get_data_dir() / "secrets.json"


def load(force: bool = False) -> dict:
    """Return the parsed secrets dict (empty on missing/corrupt). Cached after first read."""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        with open(_secrets_path(), "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def jira_creds() -> tuple[str, str] | None:
    """(email, token) if both present and non-empty, else None (Jira disabled)."""
    j = load().get("jira") or {}
    email, token = j.get("email"), j.get("token")
    if email and token:
        return email, token
    return None


def graph_ids() -> tuple[str, str] | None:
    """(client_id, tenant_id) if both present, else None (Teams disabled)."""
    g = load().get("graph") or {}
    cid, tid = g.get("client_id"), g.get("tenant_id")
    if cid and tid:
        return cid, tid
    return None


def hermes_webhook_secret() -> str | None:
    """HMAC secret used to sign ClaudeManager -> Hermes webhook events, or None.

    None means the wake-up publisher stays off (fail-closed): this is a
    different credential than the Hermes gateway read token in
    terminal/hermes_runtime.py, used in the opposite direction (this app calls
    out to Hermes' webhook platform, rather than Hermes calling in)."""
    w = load().get("hermes_webhook") or {}
    secret = w.get("secret")
    if secret:
        return secret
    return None
