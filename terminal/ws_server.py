"""
WebSocket server bridging xterm.js <-> pywinpty PTY sessions.
"""

import asyncio
import json
import secrets
import threading
import time
from urllib.parse import urlsplit
import websockets

from terminal.input_debug import log_input_boundary
from terminal.pty_manager import PtyManager


class WsCapabilityStore:
    """Short-lived, one-use capabilities bound to one managed PTY tab."""

    def __init__(self, ttl_seconds: float = 60.0, allowed_origins: set[str] | None = None):
        self.ttl_seconds = float(ttl_seconds)
        self._allow_loopback_http = allowed_origins is None
        self.allowed_origins = set(
            {"", "null", "file://", "file:///"}
            if allowed_origins is None else allowed_origins
        )
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()

    def issue(self, session_id: str, ttl_seconds: float | None = None, now: float | None = None) -> str:
        if not isinstance(session_id, str) or not session_id or "/" in session_id:
            raise ValueError("invalid session id")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._records[token] = {
                "session_id": session_id,
                "expires_at": (time.time() if now is None else now) +
                               (self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)),
                "used": False,
                "revoked": False,
            }
        return token

    def consume(self, token: str, session_id: str, origin: str | None,
                now: float | None = None) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            record = self._records.get(token)
            current = time.time() if now is None else now
            if not record or record["revoked"] or record["used"]:
                return False
            if record["session_id"] != session_id or record["expires_at"] <= current:
                return False
            if not self._origin_allowed(origin):
                return False
            record["used"] = True
            return True

    def _origin_allowed(self, origin: str | None) -> bool:
        value = origin or ""
        if value in self.allowed_origins:
            return True
        if not self._allow_loopback_http:
            return False

        # With JS API enabled, pywebview serves local files from a random
        # http://127.0.0.1:<port> origin. The WebSocket server is also loopback-only,
        # so accept that local origin without allowing arbitrary web pages.
        parsed = urlsplit(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )

    def revoke(self, token: str) -> None:
        with self._lock:
            if token in self._records:
                self._records[token]["revoked"] = True

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record["session_id"] == session_id:
                    record["revoked"] = True


class WebSocketServer:
    """Runs a WebSocket server that bridges PTY I/O to xterm.js clients."""

    def __init__(self, pty_manager: PtyManager, host: str = "127.0.0.1", port: int = 0,
                 capability_ttl: float = 60.0, allowed_origins: set[str] | None = None,
                 handshake_timeout: float = 3.0):
        self.pty_manager = pty_manager
        self.host = host
        self.port = port
        self._actual_port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self.handshake_timeout = float(handshake_timeout)
        self.capabilities = WsCapabilityStore(capability_ttl, allowed_origins)

    def issue_capability(self, session_id: str) -> str:
        return self.capabilities.issue(session_id)

    def revoke_capability(self, capability: str) -> None:
        self.capabilities.revoke(capability)

    def revoke_session(self, session_id: str) -> None:
        self.capabilities.revoke_session(session_id)

    def revoke_all(self) -> None:
        with self.capabilities._lock:
            for record in self.capabilities._records.values():
                record["revoked"] = True

    @property
    def actual_port(self) -> int:
        return self._actual_port or self.port

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        while self._actual_port is None:
            import time
            time.sleep(0.05)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self):
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
        )
        # Get the actual port (useful when port=0)
        self._actual_port = self._server.sockets[0].getsockname()[1]
        await self._server.wait_closed()

    def stop(self):
        loop, server, thread = self._loop, self._server, self._thread
        if loop and server:
            loop.call_soon_threadsafe(server.close)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._server = None
        self._loop = None
        self._thread = None

    @staticmethod
    def _request_path(websocket) -> str:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "") if request else ""
        if not path:
            path = getattr(websocket, "path", "")
        return urlsplit(path).path.strip("/")

    @staticmethod
    def _request_origin(websocket) -> str:
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", None) if request else None
        if headers is None:
            headers = getattr(websocket, "request_headers", {})
        try:
            return headers.get("Origin", "") or ""
        except AttributeError:
            return ""

    async def authenticate_handshake(self, websocket) -> bool:
        session_id = self._request_path(websocket)
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.handshake_timeout)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
            capability = message.get("capability")
            valid = (
                message.get("type") == "handshake"
                and message.get("session_id") == session_id
                and self.capabilities.consume(capability, session_id, self._request_origin(websocket))
            )
        except (asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError,
                AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            try:
                await websocket.close(1008, "Handshake rejected")
            except Exception:
                pass
        return valid

    async def _handler(self, websocket):
        session_id = self._request_path(websocket)

        session = self.pty_manager.get_session(session_id)
        if not session:
            await websocket.close(1008, "Session not found")
            return

        # No PTY reader or writer exists until the first-message capability handshake
        # succeeds. A client that knows only the private tab ID cannot touch the PTY.
        if not await self.authenticate_handshake(websocket):
            return

        # Two concurrent tasks: PTY -> WS and WS -> PTY
        read_task = asyncio.create_task(self._pty_to_ws(session, websocket))
        write_task = asyncio.create_task(self._ws_to_pty(session, websocket))

        try:
            await asyncio.gather(read_task, write_task)
        except Exception as exc:
            if isinstance(exc, websockets.exceptions.ConnectionClosed):
                print(
                    f"[ws] session={session_id} closed "
                    f"code={exc.code} reason={exc.reason!r}"
                )
            else:
                print(f"[ws] session={session_id} handler stopped ({type(exc).__name__})")
        finally:
            read_task.cancel()
            write_task.cancel()

    async def _pty_to_ws(self, session, websocket):
        """Read from PTY, send to WebSocket."""
        while True:
            try:
                data = session.read()
                if data:
                    log_input_boundary("pty->ws", data, session_id=session.id)
                    await websocket.send(data)
                elif hasattr(session, "is_alive") and not session.is_alive:
                    print(f"[ws] session={session.id} PTY exited")
                    await websocket.close(1011, "PTY exited")
                    break
                else:
                    await asyncio.sleep(0.01)
            except Exception as exc:
                print(f"[ws] session={session.id} PTY->WS stopped ({type(exc).__name__})")
                break

    async def _ws_to_pty(self, session, websocket):
        """Read from WebSocket, write to PTY."""
        async for message in websocket:
            try:
                # Check for control messages (resize)
                if isinstance(message, str) and message.startswith('{"type"'):
                    msg = json.loads(message)
                    if msg.get("type") == "resize":
                        session.resize(msg["cols"], msg["rows"])
                        continue
                text = message if isinstance(message, str) else message.decode()
                log_input_boundary("ws->pty", text, session_id=session.id)
                session.write(text)
            except Exception as exc:
                print(f"[ws] session={session.id} WS->PTY stopped ({type(exc).__name__})")
                break
