"""
Loopback HTTP endpoint the in-session Claude Code mod (mods/claudemanager-bridge)
talks to. Plan: Docs/plans/2026-10-19-ClaudeManagerMod.md.

Each managed claude tab spawned in mod mode gets its own token, injected into the
tab's env (CLAUDEMANAGER_BRIDGE_URL / CLAUDEMANAGER_BRIDGE_TOKEN) and sent back by
the mod in `X-CM-Tab-Token`. The tab is always resolved from the token, never from
the request body, so one local process holding one tab's token cannot write the
state of another tab.

Deliberately separate from terminal/agent_gateway.py: that boundary belongs to
Hermes and has its own credential. Nothing here is reachable with a Hermes token,
and nothing here approves tool calls (plan D12).

Routes (v1, all POST, JSON):
  /v1/hello  {session_id, plugin_version, engine_version, cwd}  -> marks the tab's mod alive
  /v1/event  {seq, event, session_id, tool_name?, tool_use_id?, agent_id?, aborted?, error?}
             -> hook_state.write_event, the same sink the --hook-notify path feeds
  /v1/usage  {context, rateLimits, cost?, startedAt}             -> kept in memory per tab
"""

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-CM-Tab-Token"


class TabTokenStore:
    """One live token per tab. Re-issuing for a tab (restart) revokes the old one."""

    def __init__(self):
        self._by_token: dict[str, str] = {}
        self._by_tab: dict[str, str] = {}
        self._lock = threading.Lock()

    def issue(self, tab_id: str) -> str:
        if not isinstance(tab_id, str) or not tab_id:
            raise ValueError("invalid tab id")
        token = secrets.token_urlsafe(32)
        with self._lock:
            old = self._by_tab.pop(tab_id, None)
            if old:
                self._by_token.pop(old, None)
            self._by_tab[tab_id] = token
            self._by_token[token] = tab_id
        return token

    def resolve(self, token: str | None) -> str | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            return self._by_token.get(token)

    def revoke(self, tab_id: str) -> None:
        with self._lock:
            token = self._by_tab.pop(tab_id, None)
            if token:
                self._by_token.pop(token, None)


class ModBridgeSink:
    """What the server does with each authenticated request. PtyManager implements
    the real one; tests pass a recorder."""

    def on_hello(self, tab_id: str, body: dict) -> None: ...
    def on_event(self, tab_id: str, body: dict) -> None: ...
    def on_usage(self, tab_id: str, body: dict) -> None: ...


class ModBridgeServer:
    def __init__(self, sink: ModBridgeSink | None = None, host: str = "127.0.0.1", port: int = 0):
        self.tokens = TabTokenStore()
        self.sink = sink
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("mod bridge not started")
        return f"http://{self.host}:{self._server.server_address[1]}"

    def start(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, obj: dict) -> None:
                data = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                self._reply(404, {"error": "not_found"})

            def do_POST(self):
                tab_id = bridge.tokens.resolve(self.headers.get(TOKEN_HEADER))
                if tab_id is None:
                    return self._reply(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(400, {"error": "bad_length"})
                if length > MAX_BODY:
                    return self._reply(413, {"error": "too_large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._reply(400, {"error": "bad_json"})
                if not isinstance(body, dict):
                    return self._reply(400, {"error": "bad_json"})
                route = {
                    "/v1/hello": "on_hello",
                    "/v1/event": "on_event",
                    "/v1/usage": "on_usage",
                }.get(self.path.split("?", 1)[0])
                if route is None:
                    return self._reply(404, {"error": "not_found"})
                try:
                    if bridge.sink is not None:
                        getattr(bridge.sink, route)(tab_id, body)
                except Exception as exc:
                    # The mod fails open on any non-2xx; say why without echoing input.
                    return self._reply(500, {"error": type(exc).__name__})
                self._reply(200, {"ok": True})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class TabModState:
    """Per-tab view of the mod: alive once it says hello, missing if it never does.

    `seq` guards status against out-of-order delivery: an event older than the
    newest one applied is still logged but cannot move the tab's status back."""

    def __init__(self, hello_timeout_s: float, now: float | None = None):
        self.spawned_at = time.time() if now is None else now
        self.hello_timeout_s = float(hello_timeout_s)
        self.hello: dict | None = None
        self.last_seq = 0
        self.usage: dict | None = None
        self.usage_ts: float | None = None

    def state(self, now: float | None = None) -> str:
        if self.hello is not None:
            return "alive"
        current = time.time() if now is None else now
        return "missing" if current - self.spawned_at > self.hello_timeout_s else "pending"

    def accept_seq(self, seq) -> bool:
        """True when `seq` is newer than everything applied so far (and records it)."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            return True  # no ordering info: apply, as the hooks path does
        if seq <= self.last_seq:
            return False
        self.last_seq = seq
        return True
