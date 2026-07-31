import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.bridge import Bridge


class FakeSession:
    def __init__(self, alive):
        self.is_alive = alive


class FakePtyManager:
    def __init__(self, session):
        self.session = session

    def get_session(self, session_id):
        return self.session if session_id == "tab-1" else None


class FakeWsServer:
    def __init__(self):
        self.issued_for = []

    def issue_capability(self, session_id):
        self.issued_for.append(session_id)
        return "fresh-capability"


def make_bridge(session):
    bridge = Bridge.__new__(Bridge)
    bridge.pty_manager = FakePtyManager(session)
    bridge.ws_server = FakeWsServer()
    return bridge


def test_issues_a_fresh_capability_only_for_a_live_session():
    bridge = make_bridge(FakeSession(alive=True))

    assert bridge.issue_ws_capability("tab-1") == "fresh-capability"
    assert bridge.ws_server.issued_for == ["tab-1"]


def test_does_not_issue_a_capability_for_a_dead_or_unknown_session():
    bridge = make_bridge(FakeSession(alive=False))

    assert bridge.issue_ws_capability("tab-1") is None
    assert bridge.issue_ws_capability("missing") is None
    assert bridge.ws_server.issued_for == []


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} bridge WebSocket capability checks passed")
