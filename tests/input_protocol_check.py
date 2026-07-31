import sys
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.input_protocol import (
    InputProtocolError,
    prepare_composer_message,
    prepare_terminal_paste,
)
from terminal.session_registry import SessionRegistry
from terminal.pty_manager import PtyManager
from terminal import agent_decisions
from terminal.agent_contracts import proposal_hash


def prompt_proposal():
    now = datetime.now(timezone.utc)
    payload = {
        "decision_id": str(uuid.uuid4()), "parent_decision_id": None,
        "trace_id": str(uuid.uuid4()), "version": 1,
        "context_refs": [{"source": "todo", "object_id": "todo-1",
                           "retrieved_at": now.isoformat(), "content_hash": "a" * 64,
                           "trust": "external_data"}],
        "intent": "Send an approved prompt", "action": {
            "type": "send_prompt", "target": {"session_key": "prompt-session"},
            "parameters": {"text": "hello"}, "expected_outcome": "host accepts write",
            "reversible": True,
        },
        "risk": {"level": "low", "factors": []},
        "policy": {"version": "1", "decision": "approval_required", "constraints": []},
        "model": {"provider": "openai-codex", "name": "gpt-5.6-luna", "prompt_version": "v1",
                   "attestation": {"hermes_profile": "test", "hermes_version": "0.19.0",
                                    "config_hash": "b" * 64, "session_id": "runtime"}},
        "idempotency_key": str(uuid.uuid4()), "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    return payload


def raises(fn):
    try:
        fn()
    except InputProtocolError:
        return
    raise AssertionError("expected InputProtocolError")


def test_line_endings_final_newline_and_bracketed_paste():
    assert prepare_terminal_paste("a\r\nb\rc", bracketed_paste_mode=False) == "a\rb\rc"
    assert prepare_composer_message("a\r\nb", bracketed_paste_mode=False) == "a\nb\r"
    assert prepare_composer_message("a\nb", bracketed_paste_mode=True) == "\x1b[200~a\rb\x1b[201~\r"
    assert prepare_composer_message("one", bracketed_paste_mode=True) == "one\r"
    assert prepare_composer_message("a\nb", bracketed_paste_mode=True, provider="powershell") == "a\nb\r"


def test_input_rejects_unsafe_controls_and_enforces_utf8_bytes():
    for value in ("a\x00b", "a\x1bb", "a\x01b", "a\x7fb"):
        raises(lambda value=value: prepare_composer_message(value))
    raises(lambda: prepare_composer_message("á" * 10, max_bytes=10))


class Session:
    id = "private-pty"
    session_key = "public-session"
    agent_session_id = "provider-session"
    claude_session_id = "claude-session"
    terminal_type = "claude"


def test_session_registry_aliases_and_idempotent_close():
    registry = SessionRegistry()
    session = Session()
    registry.register(session)
    assert registry.find_by_session_key("public-session") is session
    assert registry.find_alias("private-pty") is session
    assert registry.find_alias("provider-session") is session
    lock = registry.lock_for("public-session")
    assert lock is registry.lock_for("public-session")
    assert registry.begin_close("public-session") is True
    assert registry.begin_close("public-session") is False
    registry.finish_close("public-session")
    assert registry.find_by_session_key("public-session") is None
    registry.finish_close("public-session")


class PromptSession:
    id = "private-pty"
    session_key = "prompt-session"
    agent_session_id = "provider-session"
    claude_session_id = "claude-session"
    terminal_type = "claude"
    def __init__(self): self.writes = []
    def write(self, text): self.writes.append(text); return True


def test_approved_prompt_writes_once_and_duplicate_is_receipt_only():
    with tempfile.TemporaryDirectory() as root:
        os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = root
        proposal = prompt_proposal()
        agent_decisions.create_proposal(proposal)
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-prompt")
        manager = PtyManager.__new__(PtyManager)
        manager.registry = SessionRegistry()
        manager.sessions = {}
        session = PromptSession()
        manager.registry.register(session)
        first = manager.send_prompt("prompt-session", "hello", "request-1",
                                    proposal["decision_id"], proposal["proposal_hash"])
        second = manager.send_prompt("prompt-session", "hello", "request-1",
                                     proposal["decision_id"], proposal["proposal_hash"])
        assert first["state"] == "SENT"
        assert second["state"] == "SENT"
        assert session.writes == ["hello\r"]
if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} input protocol checks passed")
