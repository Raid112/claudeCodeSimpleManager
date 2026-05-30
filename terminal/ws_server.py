"""
WebSocket server bridging xterm.js <-> pywinpty PTY sessions.
"""

import asyncio
import json
import threading
import websockets

from terminal.input_debug import log_input_boundary
from terminal.pty_manager import PtyManager


class WebSocketServer:
    """Runs a WebSocket server that bridges PTY I/O to xterm.js clients."""

    def __init__(self, pty_manager: PtyManager, host: str = "127.0.0.1", port: int = 0):
        self.pty_manager = pty_manager
        self.host = host
        self.port = port
        self._actual_port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

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
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
        )
        # Get the actual port (useful when port=0)
        self._actual_port = server.sockets[0].getsockname()[1]
        await server.serve_forever()

    async def _handler(self, websocket):
        # Extract session_id from path: /session_id
        path = websocket.request.path if hasattr(websocket, 'request') else ""
        session_id = path.strip("/")

        session = self.pty_manager.get_session(session_id)
        if not session:
            await websocket.close(1008, "Session not found")
            return

        # Two concurrent tasks: PTY -> WS and WS -> PTY
        read_task = asyncio.create_task(self._pty_to_ws(session, websocket))
        write_task = asyncio.create_task(self._ws_to_pty(session, websocket))

        try:
            await asyncio.gather(read_task, write_task)
        except (websockets.exceptions.ConnectionClosed, Exception):
            pass
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
                else:
                    await asyncio.sleep(0.01)
            except Exception:
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
            except Exception:
                break
