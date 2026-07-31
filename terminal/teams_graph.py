"""
Teams via Microsoft Graph — lean copy of teams-copilot/graph.py, on-demand only.

Kept from the original: device-code auth (silent + interactive at startup), _get,
parse_message, me_id. DROPPED: the infinite message_source poller and the persistent
.graph_seen.json dedup (those served "suggest a reply per new msg" — here we just list
recent chats for the link popover, one-shot, no state between calls).

Creds (client_id, tenant_id) come from secrets.json (see secrets_store); reusing the
teams-copilot app registration means the existing consent + .msal_cache.json still apply.
Degrades gracefully: no creds -> available() is False, Teams features off.
"""

import re
import time
import threading
from html import unescape
from pathlib import Path

from terminal import secrets_store
from terminal.hook_state import get_data_dir

SCOPES = ["Chat.Read", "User.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"

_me_id_cache: str | None = None

# Short-lived cache for list_recent: the popover loads all recent chats + their last
# messages (a few seconds of parallel fetches), so we hold the result briefly to keep
# reopens and keystroke re-renders instant. TTL kept small so new messages surface fast.
_recent_cache: dict = {"ts": 0.0, "key": None, "data": None}
_RECENT_TTL = 120.0  # seconds
_refresh_lock = threading.Lock()


def _cache_path() -> Path:
    return get_data_dir() / ".msal_cache.json"


def available() -> bool:
    return secrets_store.graph_ids() is not None


# ---------- auth (device-code) ----------
def _token(interactive: bool = False) -> str:
    """Access token. interactive=True allows device-flow (startup only, main thread).
    On-demand calls use interactive=False: silent refresh, or raise (never block on a
    device prompt nobody sees)."""
    import msal

    ids = secrets_store.graph_ids()
    if ids is None:
        raise RuntimeError("Graph credentials missing (secrets.json)")
    client_id, tenant_id = ids

    cache = msal.SerializableTokenCache()
    cp = _cache_path()
    if cp.exists():
        cache.deserialize(cp.read_text())
    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        if not interactive:
            raise RuntimeError("Graph token expired and no interactive login — restart the app")
        flow = app.initiate_device_flow(scopes=SCOPES)
        print(flow["message"])  # user opens the URL and types the code
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        cp.write_text(cache.serialize())
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Graph auth failed"))
    return result["access_token"]


def ensure_auth() -> bool:
    """Guarantee a token at startup (main thread), device-flow if needed. No-op without creds.
    Returns True if authenticated. Best-effort: never raises to the caller."""
    if not available():
        return False
    try:
        _token(interactive=True)
        return True
    except Exception as exc:
        print(f"[teams_graph] auth failed: {exc}")
        return False


def _get(path: str, params: dict | None = None, token: str | None = None) -> dict:
    import httpx

    tok = token or _token()  # reuse a token across parallel calls (avoid MSAL cache races)
    r = httpx.get(f"{GRAPH}{path}", params=params,
                  headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    import httpx

    r = httpx.post(f"{GRAPH}{path}", json=body,
                   headers={"Authorization": f"Bearer {_token()}",
                            "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def me_id() -> str:
    global _me_id_cache
    if _me_id_cache is None:
        _me_id_cache = _get("/me").get("id", "")
    return _me_id_cache


# ---------- parsing (pure) ----------
# Whitespace the Graph payload smuggles in: literal \n between block tags, &nbsp; (\xa0) from
# the Teams composer, and zero-width junk from pastes. All of it breaks substring search — the
# popover's search box is a single-line <input>, so pasting a multi-line message strips the
# newlines and the paste stops matching the stored text. Collapse everything to one space here
# (at the source), so preview/title are single-line too.
_WS_RE = re.compile(r"[\s ​‌‍﻿]+")
_BLOCK_END_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", re.I)


def _html_to_text(html: str) -> str:
    txt = _BLOCK_END_RE.sub(" ", html or "")  # block boundary -> space (never glue words)
    txt = re.sub(r"<[^>]+>", "", txt)         # strip remaining tags (incl <at>mentions</at>)
    return _WS_RE.sub(" ", unescape(txt)).strip()


def parse_message(raw: dict, chat: dict) -> dict:
    """chatMessage + chat -> flat dict for the link popover."""
    sender = (raw.get("from") or {}).get("user") or {}
    return {
        "chat_id": chat.get("id", ""),
        "chat_type": chat.get("chatType", "group"),  # 'oneOnOne'|'group'|'meeting'
        "chat_name": chat.get("topic") or "(DM)",
        "sender_id": sender.get("id", ""),
        "sender_name": sender.get("displayName") or sender.get("id", "?"),
        "text": _html_to_text((raw.get("body") or {}).get("content", "")),
        "msg_id": raw.get("id"),
        "ts": raw.get("createdDateTime"),
    }


# ---------- one-shot recent chats + shallow message load (for content search) ----------
def _msg_text(m: dict) -> str:
    """Displayable text for a chatMessage. Falls back to a bracketed placeholder for
    attachment-only messages (audio/video/image) so they DON'T silently vanish — a chat
    that ended in a voice note must still surface a linkable line. '' only when truly empty."""
    body = m.get("body") or {}
    txt = _html_to_text(body.get("content", ""))
    if txt:
        return txt
    content = body.get("content") or ""
    # messageReference is a reply wrapper; its own comment (if any) is in the <p> above and
    # was already caught by _html_to_text — ignore it when deciding the placeholder.
    atts = [a for a in (m.get("attachments") or [])
            if (a.get("contentType") or "") != "messageReference"]
    for a in atts:
        ct = (a.get("contentType") or "").lower()
        if "audio" in ct:
            return "[áudio]"
        if "video" in ct:
            return "[vídeo]"
    if "<img" in content:
        return "[imagem]"
    if atts:
        return "[anexo]"
    return ""


def _chat_messages(chat_id: str, token: str, top: int = 50, me: str = "") -> list[dict]:
    """Last `top` real messages of a chat, newest first:
    {sender_id, sender_name, text, ts, from_me}. Chat.Read covers 1:1, group and meeting
    chats (NOT team channels). Attachment-only msgs become '[áudio]'/'[vídeo]'/'[imagem]'.
    [] on error (best-effort). `top` is capped at 50 by Graph on this endpoint."""
    try:
        data = _get(f"/me/chats/{chat_id}/messages", {"$top": top}, token=token)
    except Exception:
        return []
    out = []
    for m in data.get("value", []):
        if m.get("messageType") != "message":
            continue  # skip system events (member added, call started, etc.)
        text = _msg_text(m)
        if not text:
            continue
        sender = ((m.get("from") or {}).get("user") or {})
        sid = sender.get("id")
        out.append({
            "msg_id": m.get("id"),
            "sender_id": sid,
            "sender_name": sender.get("displayName"),
            "text": text,
            "ts": m.get("createdDateTime"),
            # me=="" means me_id() failed: degrade to from_me=False for everyone rather than
            # mislabel — a missing id must never make MY messages look like the other person's.
            "from_me": bool(me) and sid == me,
        })
    return out


def _fetch_recent(top: int, per_chat: int) -> list[dict]:
    """The actual work behind list_recent: /me/chats + parallel per-chat message loads.
    Raises on hard failure (caller decides whether to serve stale)."""
    try:
        me = me_id()
    except Exception:
        me = ""
    token = _token()
    data = _get("/me/chats", {
        "$expand": "lastMessagePreview",
        "$orderby": "lastMessagePreview/createdDateTime desc",
        "$top": top,
    }, token=token)

    chats = [c for c in data.get("value", []) if c.get("lastMessagePreview")]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=15) as ex:
        msgs_per_chat = list(ex.map(lambda c: _chat_messages(c["id"], token, per_chat, me), chats))

    out: list[dict] = []
    for chat, msgs in zip(chats, msgs_per_chat):
        topic = chat.get("topic")
        # linkable lines = only the OTHER people's messages, newest first (never mine).
        others = [m for m in msgs if not m["from_me"]]
        last_other = others[0] if others else None
        person = topic or (last_other["sender_name"] if last_other else None) or "(conversa)"
        preview = last_other["text"] if last_other else ""  # the other's last msg; never mine
        out.append({
            "person": person,
            "preview": preview,
            "msg_id": last_other["msg_id"] if last_other else None,
            "chat_id": chat["id"],
            "chat_type": chat.get("chatType", "group"),
            "chat_name": topic or "",
            "ts": (chat.get("lastMessagePreview") or {}).get("createdDateTime"),
            # per-message lines for the popover: filter/link on any single message, not just
            # the last. sender_name matters for groups (link shows who said it, not the topic).
            "messages": [
                {
                    "msg_id": m["msg_id"],
                    "sender_name": m["sender_name"],
                    "text": m["text"],
                    "ts": m["ts"],
                }
                for m in others
            ],
        })
    return out


def _refresh_recent_async(top: int, per_chat: int) -> None:
    """Refresh the cache in the background (one at a time). Used for stale-while-revalidate
    so the popover never blocks on a full reload once it has any cached data."""
    import threading
    if not _refresh_lock.acquire(blocking=False):
        return  # a refresh is already running
    def run():
        try:
            out = _fetch_recent(top, per_chat)
            _recent_cache.update({"ts": time.time(), "key": (top, per_chat), "data": out})
        except Exception as exc:
            print(f"[teams_graph] background refresh failed: {exc}")
        finally:
            _refresh_lock.release()
    threading.Thread(target=run, daemon=True).start()


def list_recent(top: int = 30, per_chat: int = 50, force: bool = False) -> list[dict]:
    """Recent chats WITH their last `per_chat` messages, for the link popover's content
    search. [] if Teams off or on error.

    Per chat we derive: the other person's name (1:1) or topic (group), a preview that is
    always the other person's last message (never mine), and `messages` — the other people's
    recent messages (newest first) so the popover can filter and link on any single message,
    not just the last one. `per_chat=50` is the Graph page cap — the widest single-request
    window, so a message from earlier in the day still shows up in the search.

    Cache strategy (a full load is ~several seconds of parallel fetches):
    - fresh cache (< TTL): return it.
    - stale cache: return it immediately AND refresh in the background (stale-while-
      revalidate) — the popover stays instant, data catches up on the next open.
    - no cache: block once and fetch (the boot warm-up normally beats the first open).
    Pass force=True to bypass and reload synchronously."""
    if not available():
        return []
    key = (top, per_chat)
    have = _recent_cache["data"] is not None and _recent_cache["key"] == key
    age = time.time() - _recent_cache["ts"]
    if have and not force and age < _RECENT_TTL:
        return _recent_cache["data"]
    if have and not force:
        _refresh_recent_async(top, per_chat)   # stale: serve now, refresh behind
        return _recent_cache["data"]
    try:
        out = _fetch_recent(top, per_chat)
    except Exception as exc:
        print(f"[teams_graph] list_recent failed: {exc}")
        return _recent_cache["data"] or []     # serve stale on transient failure
    _recent_cache.update({"ts": time.time(), "key": key, "data": out})
    return out


# ---------- full-text search across chat messages ----------
def search_messages(query: str, top: int = 25) -> list[dict]:
    """Search ALL chat messages by text via the Graph Search API (not just the recent
    previews list_recent returns). [] if Teams disabled, empty query, or any error."""
    query = (query or "").strip()
    if not available() or not query:
        return []
    try:
        data = _post("/search/query", {
            "requests": [{
                "entityTypes": ["chatMessage"],
                "query": {"queryString": query},
                "from": 0,
                "size": top,
            }]
        })
    except Exception as exc:
        print(f"[teams_graph] search_messages failed: {exc}")
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for resp in data.get("value", []):
        for container in resp.get("hitsContainers", []):
            for hit in container.get("hits", []):
                res = hit.get("resource") or {}
                # The search hit's resource IS a chatMessage; synthesize the chat wrapper
                # (search doesn't return the chat topic) so parse_message can flatten it.
                chat = {"id": res.get("chatId", ""), "chatType": "oneOnOne", "topic": None}
                msg = parse_message(res, chat)
                key = msg["msg_id"] or (msg["chat_id"] + (msg["ts"] or ""))
                if key in seen:
                    continue
                seen.add(key)
                text = msg["text"] or _html_to_text(hit.get("summary", ""))
                if not text:
                    continue
                out.append({
                    "person": msg["sender_name"],
                    "preview": text,
                    "msg_id": msg["msg_id"],
                    "chat_id": msg["chat_id"],
                    "chat_type": msg["chat_type"],
                    "chat_name": msg["chat_name"],
                    "ts": msg["ts"],
                })
    return out
