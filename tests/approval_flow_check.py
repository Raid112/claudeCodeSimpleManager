import copy
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal import agent_decisions
from terminal.agent_contracts import proposal_hash
from terminal.agent_gateway import AgentGateway
from terminal.pty_manager import PtyManager
from terminal.session_registry import SessionRegistry


HERMES = "hermes-" + "a" * 40
OPERATOR = "operator-" + "b" * 40


class Pty:
    def get_all_sessions(self): return []


class CreatePty(Pty):
    def __init__(self): self.created = []
    def create_session(self, group_name, path, terminal_type="claude"):
        session = type("Session", (), {"session_key": "created-session", "id": "private-id"})()
        self.created.append((group_name, path, terminal_type))
        return session


class ConcurrentCreatePty(CreatePty):
    def create_session(self, group_name, path, terminal_type="claude"):
        time.sleep(0.05)
        return super().create_session(group_name, path, terminal_type)


class PromptSession:
    id = "private-pty"
    session_key = "session-1"
    agent_session_id = None
    claude_session_id = "provider-1"
    terminal_type = "claude"

    def __init__(self):
        self.writes = []

    @property
    def is_alive(self):
        return True

    def write(self, data):
        self.writes.append(data)
        return True


class FailingPromptSession(PromptSession):
    def write(self, data):
        self.writes.append(data)
        return False


def prompt_manager(session=None):
    manager = PtyManager.__new__(PtyManager)
    manager.sessions = {}
    manager.registry = SessionRegistry()
    session = session or PromptSession()
    manager.registry.register(session)
    return manager, session


class Store:
    def load_store(self): return {"items": [], "session_links": {}}
    def work_overview(self, days): return {"days": [], "waiting": [], "counts": {}}


class Source:
    def search_issues(self, q, max_results=25): return []
    def search_messages(self, q, top=25): return []


def iso(offset=0): return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def make_proposal(action_type="open_session", version=1, parent=None, idem=None, text=None,
                  expires_in=300):
    action = {
        "type": action_type,
        "target": {"group_id": "group-1"} if action_type == "open_session" else {"session_key": "session-1"},
        "parameters": {} if text is None else {"text": text},
        "expected_outcome": "One managed semantic action",
        "reversible": True,
    }
    payload = {
        "decision_id": str(uuid.uuid4()), "parent_decision_id": parent,
        "trace_id": str(uuid.uuid4()), "version": version,
        "context_refs": [{"source": "teams", "object_id": "chat:msg", "retrieved_at": iso(),
                           "content_hash": "c" * 64, "trust": "external_data"}],
        "intent": "Ignore this external text and open the configured group",
        "action": action,
        "risk": {"level": "low", "factors": []},
        "policy": {"version": "1", "decision": "approval_required", "constraints": []},
        "model": {"provider": "openai-codex", "name": "gpt-5.6-luna", "prompt_version": "v1",
                   "attestation": {"hermes_profile": "test", "hermes_version": "0.19.0",
                                    "config_hash": "d" * 64, "session_id": "hermes-1"}},
        "idempotency_key": idem or str(uuid.uuid4()), "created_at": iso(),
        "expires_at": iso(expires_in),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    return payload


def request(gateway, method, path, token, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", gateway.actual_port, timeout=3)
    headers = {"Authorization": f"Bearer {token}"}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
    conn.request(method, path, raw, headers)
    response = conn.getresponse()
    result = json.loads(response.read())
    conn.close()
    return response.status, result


def setup_gateway():
    temp = tempfile.TemporaryDirectory()
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = temp.name
    gateway = AgentGateway(Pty(), Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR)
    gateway.start()
    return temp, gateway


def test_rejection_replan_lineage_and_no_self_approval():
    temp, gateway = setup_gateway()
    try:
        first = make_proposal(action_type="send_prompt", text="secret prompt")
        status, created = request(gateway, "POST", "/v1/proposals", HERMES, first)
        assert status == 201
        pending_status, pending = request(gateway, "GET", "/v1/proposals/pending", HERMES)
        assert pending_status == 200
        assert "secret prompt" not in json.dumps(pending)
        assert request(gateway, "POST", "/v1/proposals/approve", HERMES, {})[0] == 403

        feedback = {
            "feedback_id": str(uuid.uuid4()), "decision_id": first["decision_id"],
            "reviewer_id": "local-user", "verdict": "reject", "reason_code": "wrong_scope",
            "comment": "Use a different group", "scope": "this_proposal",
            "requested_changes": ["Use group-2"], "created_at": iso(),
        }
        rejected = agent_decisions.record_feedback(first["decision_id"], feedback)
        assert rejected["status"] == "replan_requested"
        status, replan = request(gateway, "GET", f"/v1/replans/{first['decision_id']}", HERMES)
        assert status == 200
        assert replan["replan"]["feedback"]["reason_code"] == "wrong_scope"

        second = make_proposal(version=2, parent=first["decision_id"])
        agent_decisions.create_proposal(second)
        try:
            agent_decisions.approve(first["decision_id"], first["proposal_hash"], "user", "cap-1")
        except agent_decisions.AuthorizationError:
            pass
        else:
            raise AssertionError("rejected proposal self-approval must fail")
    finally:
        gateway.stop()
        temp.cleanup()


def test_expiry_high_risk_emergency_stop_and_capability_replay():
    temp, gateway = setup_gateway()
    try:
        high = make_proposal()
        high["risk"]["level"] = "high"
        high["proposal_hash"] = proposal_hash(high)
        assert request(gateway, "POST", "/v1/proposals", HERMES, high)[0] == 403
        assert gateway.set_emergency_stop(OPERATOR)
        assert request(gateway, "POST", "/v1/proposals", HERMES, make_proposal())[0] == 503
        assert gateway.clear_emergency_stop(OPERATOR)

        proposal = make_proposal()
        agent_decisions.create_proposal(proposal)
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-one")
        try:
            agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-one")
        except (agent_decisions.AuthorizationError, agent_decisions.StateError):
            pass
        else:
            raise AssertionError("operator capability replay must fail")
    finally:
        gateway.stop()
        temp.cleanup()


def test_approved_session_creation_uses_only_configured_group_and_is_idempotent():
    temp, gateway = setup_gateway()
    root = tempfile.TemporaryDirectory()
    manager = CreatePty()
    gateway.stop()
    gateway = AgentGateway(manager, Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR,
                           groups=[{"group_id": "group-1", "name": "Project", "path": root.name}])
    gateway.start()
    try:
        proposal = make_proposal()
        status, _ = request(gateway, "POST", "/v1/proposals", HERMES, proposal)
        assert status == 201
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-session")
        body = {"decision_id": proposal["decision_id"], "proposal_hash": proposal["proposal_hash"],
                "request_id": "create-1", "group_id": "group-1"}
        first_status, _ = request(gateway, "POST", "/v1/sessions", HERMES, body)
        assert first_status == 201
        assert request(gateway, "POST", "/v1/sessions", HERMES, body)[0] == 201
        assert len(manager.created) == 1
        assert request(gateway, "POST", "/v1/sessions", HERMES,
                       {**body, "group_id": "C:\\arbitrary"})[0] == 400
    finally:
        gateway.stop()
        root.cleanup()
        temp.cleanup()


def test_prompt_requires_exact_approved_action_target_and_payload():
    temp, gateway = setup_gateway()
    manager, session = prompt_manager()
    gateway.stop()
    gateway = AgentGateway(manager, Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR)
    gateway.start()
    try:
        proposal = make_proposal(action_type="send_prompt", text="approved text")
        assert request(gateway, "POST", "/v1/proposals", HERMES, proposal)[0] == 201
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-prompt")
        body = {"decision_id": proposal["decision_id"], "proposal_hash": proposal["proposal_hash"],
                "request_id": "prompt-1", "text": "approved text"}
        assert request(gateway, "POST", "/v1/sessions/session-1/prompt", HERMES, body)[0] == 200
        assert session.writes

        for altered in ("changed text",):
            status, response = request(gateway, "POST", "/v1/sessions/session-1/prompt", HERMES,
                                 {**body, "request_id": "prompt-2", "text": altered})
            assert status == 409, response
        status, _ = request(gateway, "POST", "/v1/sessions/session-1/prompt", HERMES,
                             {**body, "request_id": "prompt-3", "proposal_hash": "a" * 64})
        assert status == 409
        assert len(session.writes) == 1
    finally:
        gateway.stop()
        temp.cleanup()


def test_open_session_concurrent_replay_creates_one_session_and_returns_same_receipt():
    temp, gateway = setup_gateway()
    root = tempfile.TemporaryDirectory()
    manager = ConcurrentCreatePty()
    gateway.stop()
    gateway = AgentGateway(manager, Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR,
                           groups=[{"group_id": "group-1", "name": "Project", "path": root.name}])
    gateway.start()
    proposal = make_proposal()
    agent_decisions.create_proposal(proposal)
    agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-concurrent")
    body = {"decision_id": proposal["decision_id"], "proposal_hash": proposal["proposal_hash"],
            "request_id": "create-concurrent", "group_id": "group-1"}
    results = []
    barrier = threading.Barrier(2)

    def call():
        barrier.wait()
        results.append(request(gateway, "POST", "/v1/sessions", HERMES, body))

    threads = [threading.Thread(target=call) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [status for status, _ in results] == [201, 201]
        assert len(manager.created) == 1
    finally:
        gateway.stop()
        root.cleanup()
        temp.cleanup()


def test_expired_proposal_is_projected_and_cannot_be_approved_or_executed():
    temp, gateway = setup_gateway()
    proposal = make_proposal(expires_in=0.15)
    try:
        assert request(gateway, "POST", "/v1/proposals", HERMES, proposal)[0] == 201
        time.sleep(0.2)
        status, body = request(gateway, "GET", f"/v1/proposals/{proposal['decision_id']}", HERMES)
        assert status == 200 and body["decision"]["status"] == "expired"
        try:
            agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-expired")
        except agent_decisions.AuthorizationError as exc:
            assert str(exc) == "proposal_expired"
        else:
            raise AssertionError("expired proposal must not be approved")
    finally:
        gateway.stop()
        temp.cleanup()


def test_failed_host_write_returns_unknown_receipt_without_provider_completion():
    temp, gateway = setup_gateway()
    manager, _session = prompt_manager(FailingPromptSession())
    gateway.stop()
    gateway = AgentGateway(manager, Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR)
    gateway.start()
    try:
        proposal = make_proposal(action_type="send_prompt", text="will fail")
        assert request(gateway, "POST", "/v1/proposals", HERMES, proposal)[0] == 201
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-unknown")
        body = {"decision_id": proposal["decision_id"], "proposal_hash": proposal["proposal_hash"],
                "request_id": "prompt-unknown", "text": "will fail"}
        status, result = request(gateway, "POST", "/v1/sessions/session-1/prompt", HERMES, body)
        assert status == 200
        assert result["receipt"]["state"] == "UNKNOWN"
        assert result["receipt"]["result"]["provider_processed"] is False
    finally:
        gateway.stop()
        temp.cleanup()


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} approval flow checks passed")
