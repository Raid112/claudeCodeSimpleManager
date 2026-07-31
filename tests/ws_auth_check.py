import asyncio
import json
import sys
from pathlib import Path
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.ws_server import WebSocketServer, WsCapabilityStore


def test_capabilities_bind_tab_expire_and_replay():
    store = WsCapabilityStore(ttl_seconds=30, allowed_origins={"null", "http://127.0.0.1"})
    token = store.issue("private-tab-1", now=100.0)
    assert store.consume(token, "private-tab-1", "null", now=101.0)
    assert not store.consume(token, "private-tab-1", "null", now=102.0)

    other = store.issue("private-tab-1", now=100.0)
    assert not store.consume(other, "private-tab-2", "null", now=101.0)
    expired = store.issue("private-tab-1", ttl_seconds=1, now=100.0)
    assert not store.consume(expired, "private-tab-1", "null", now=102.0)
    wrong_origin = store.issue("private-tab-1", now=100.0)
    assert not store.consume(wrong_origin, "private-tab-1", "https://evil.invalid", now=101.0)


def test_pywebview_loopback_origin_is_allowed_but_remote_origin_is_not():
    store = WsCapabilityStore(ttl_seconds=30)
    local = store.issue("tab-1", now=100.0)
    assert store.consume(local, "tab-1", "http://127.0.0.1:50382", now=101.0)

    remote = store.issue("tab-1", now=100.0)
    assert not store.consume(remote, "tab-1", "http://127.0.0.2:50382", now=101.0)


def test_revoke_and_active_capability_cleanup():
    store = WsCapabilityStore(ttl_seconds=30)
    token = store.issue("tab-1")
    store.revoke(token)
    assert not store.consume(token, "tab-1", "null")
    token2 = store.issue("tab-1")
    store.revoke_session("tab-1")
    assert not store.consume(token2, "tab-1", "null")


class FakeRequest:
    def __init__(self, path, origin="null"):
        self.path = path
        self.headers = {"Origin": origin}


class FakeWebSocket:
    def __init__(self, message, path="/tab-1", origin="null"):
        self.request = FakeRequest(path, origin)
        self.message = message
        self.closed = None

    async def recv(self):
        return self.message

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)


def test_handshake_rejects_raw_input_and_wrong_tab_before_pty_tasks():
    server = WebSocketServer(object(), allowed_origins={"null"}, handshake_timeout=0.1)
    token = server.issue_capability("tab-1")
    raw = FakeWebSocket("whoami")
    assert not asyncio.run(server.authenticate_handshake(raw))
    assert raw.closed and raw.closed[0] == 1008

    wrong = FakeWebSocket(json.dumps({
        "type": "handshake", "session_id": "tab-2", "capability": token,
    }))
    assert not asyncio.run(server.authenticate_handshake(wrong))
    assert wrong.closed and wrong.closed[0] == 1008


def test_legitimate_handshake_is_one_time_and_does_not_use_url_secret():
    server = WebSocketServer(object(), allowed_origins={"null"}, handshake_timeout=0.1)
    token = server.issue_capability("tab-1")
    client = FakeWebSocket(json.dumps({
        "type": "handshake", "session_id": "tab-1", "capability": token,
    }))
    assert asyncio.run(server.authenticate_handshake(client))
    assert "capability" not in client.request.path
    replay = FakeWebSocket(json.dumps({
        "type": "handshake", "session_id": "tab-1", "capability": token,
    }))
    assert not asyncio.run(server.authenticate_handshake(replay))


class LiveSession:
    id = "tab-1"
    writes = []
    def read(self): return ""
    def write(self, text): self.writes.append(text)
    def resize(self, cols, rows): pass


class LiveManager:
    def __init__(self): self.session = LiveSession()
    def get_session(self, session_id): return self.session if session_id == "tab-1" else None


def test_live_server_never_writes_before_handshake_and_writes_after_legitimate_handshake():
    manager = LiveManager()
    server = WebSocketServer(manager, port=0, allowed_origins={"null"}, handshake_timeout=0.3)
    server.start()

    async def exercise():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.actual_port}/tab-1", origin="null") as client:
                await client.send("not-a-handshake")
                try:
                    await client.recv()
                except websockets.exceptions.ConnectionClosed:
                    pass
            assert manager.session.writes == []

            token = server.issue_capability("tab-1")
            async with websockets.connect(f"ws://127.0.0.1:{server.actual_port}/tab-1", origin="null") as client:
                await client.send(json.dumps({"type": "handshake", "session_id": "tab-1", "capability": token}))
                await client.send("legitimate")
                await asyncio.sleep(0.05)
            assert manager.session.writes == ["legitimate"]
        finally:
            server.stop()

    asyncio.run(exercise())


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} websocket auth checks passed")
