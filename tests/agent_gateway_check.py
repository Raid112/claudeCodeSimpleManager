import http.client
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.agent_gateway import AgentGateway
from terminal.agent_contracts import proposal_hash

HERMES_OLD = "hermes-old-" + "a" * 32
HERMES_NEW = "hermes-new-" + "b" * 32
OPERATOR = "operator-" + "c" * 32
_DECISION_ROOTS = []


class FakePtyManager:
    def __init__(self):
        self.prompts = []

    def get_all_sessions(self):
        return [{
            "id": "private-pty-id",
            "session_key": "session-public-1",
            "group_name": "Project",
            "path": r"C:\secret\project",
            "is_alive": True,
            "claude_session_id": "private-provider-id",
            "agent_session_id": None,
            "terminal_type": "claude",
            "state": "ready",
            "state_ts": 123.0,
        }]

    def send_prompt(self, session_key, text, request_id, decision_id, proposal_hash):
        self.prompts.append((session_key, text, request_id, decision_id, proposal_hash))
        return {"request_id": request_id, "state": "SENT", "provider_processed": False}


class FakeWorkItems:
    def load_store(self):
        return {
            "version": 1,
            "items": [{
                "id": "wi_1", "source": "teams", "title": "untrusted <script>alert(1)</script>",
                "external_key": "teams:chat:msg", "external_url": "https://example.invalid/item",
                "status": "open", "person": "Someone", "done": False, "archived": False,
                "workflow_state": "active", "created_at": 1.0, "closed_at": None,
            }],
            "session_links": {
                "session-public-1": {"wi_id": "wi_1", "group_name": "Project",
                                     "path": r"C:\secret\project", "name": "tab"}
            },
        }

    def work_overview(self, days):
        return {"days": [], "waiting": [], "counts": {"active": 1, "waiting": 0,
                                                            "completed_today": 0}}


class FakeJira:
    def search_issues(self, query, max_results=25):
        return [{"external_key": "DS-1", "title": query, "status": "Open"}]


class FakeTeams:
    def search_messages(self, query, top=25):
        return [{"chat_id": "chat-1", "msg_id": "msg-1", "text": query}]


class BrokenJira:
    def search_issues(self, query, max_results=25):
        raise RuntimeError("source unavailable")


def proposal_payload(action_type="send_prompt", *, decision_id=None, text="secret prompt",
                     expires_in=300):
    target = {"session_key": "session-public-1"}
    parameters = {"text": text}
    if action_type == "open_session":
        target = {"group_id": "group-1"}
        parameters = {}
    payload = {
        "decision_id": decision_id or str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "version": 1,
        "context_refs": [{"source": "session", "object_id": "session-public-1",
                           "retrieved_at": datetime.now(timezone.utc).isoformat(),
                           "content_hash": "c" * 64, "trust": "external_data"}],
        "intent": "Perform one approved semantic action",
        "action": {"type": action_type, "target": target, "parameters": parameters,
                    "expected_outcome": "Host accepts one action", "reversible": True},
        "risk": {"level": "low", "factors": []},
        "policy": {"version": "1", "decision": "approval_required", "constraints": []},
        "model": {"provider": "openai-codex", "name": "gpt-5.6-luna",
                   "prompt_version": "v1", "attestation": {
                       "hermes_profile": "test", "hermes_version": "0.19.0",
                       "config_hash": "d" * 64, "session_id": "runtime-1"}},
        "idempotency_key": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    return payload


def request(gateway, method, path, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", gateway.actual_port, timeout=3)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    conn.request(method, path, body=encoded, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body)


def make_gateway(**kwargs):
    root = tempfile.TemporaryDirectory()
    _DECISION_ROOTS.append(root)
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = root.name
    jira = kwargs.pop("jira_adapter", FakeJira())
    return AgentGateway(
        FakePtyManager(), FakeWorkItems(), jira, FakeTeams(),
        host="127.0.0.1", port=0, hermes_token=HERMES_OLD, operator_token=OPERATOR,
        **kwargs,
    )


def test_auth_rotation_revocation_and_health_redaction():
    gateway = make_gateway()
    gateway.start()
    try:
        status, _ = request(gateway, "GET", "/v1/health")
        assert status == 401
        status, health = request(gateway, "GET", "/v1/health", HERMES_OLD)
        assert status == 200
        assert health["ok"] is True
        encoded = json.dumps(health)
        assert HERMES_OLD not in encoded and "environment" not in encoded.lower()
        new_token = gateway.rotate_hermes_token(HERMES_NEW)
        assert new_token == HERMES_NEW
        assert request(gateway, "GET", "/v1/health", HERMES_OLD)[0] == 401
        assert request(gateway, "GET", "/v1/health", HERMES_NEW)[0] == 200
    finally:
        gateway.stop()


def test_read_routes_are_redacted_and_sources_are_bounded():
    gateway = make_gateway()
    gateway.start()
    try:
        status, sessions = request(gateway, "GET", "/v1/sessions", HERMES_OLD)
        assert status == 200
        encoded = json.dumps(sessions)
        assert "session-public-1" in encoded
        assert "private-pty-id" not in encoded
        assert "C:\\secret" not in encoded
        assert "private-provider-id" not in encoded

        status, items = request(gateway, "GET", "/v1/work-items", HERMES_OLD)
        assert status == 200
        assert "C:\\secret" not in json.dumps(items)
        assert "untrusted <script>" in json.dumps(items)
        assert request(gateway, "GET", "/v1/jira/search?q=DS-1", HERMES_OLD)[0] == 200
        assert request(gateway, "GET", "/v1/teams/search?q=hello", HERMES_OLD)[0] == 200
        assert request(gateway, "GET", "/v1/jira/search?q=" + ("x" * 500), HERMES_OLD)[0] == 413
    finally:
        gateway.stop()


def test_unknown_route_source_error_and_sensitive_hermes_denial():
    gateway = make_gateway()
    gateway.start()
    try:
        assert request(gateway, "GET", "/v1/not-here", HERMES_OLD)[0] == 404
        assert request(gateway, "POST", "/v1/health", HERMES_OLD)[0] == 405
        assert request(gateway, "POST", "/v1/proposals/approve", HERMES_OLD)[0] == 403
        assert request(gateway, "GET", "/v1/health?token=" + HERMES_OLD, HERMES_OLD)[0] == 400
    finally:
        gateway.stop()


def test_source_error_is_service_unavailable_and_emergency_stop_is_operator_only():
    gateway = make_gateway(jira_adapter=BrokenJira())
    gateway.start()
    try:
        assert request(gateway, "GET", "/v1/jira/search?q=DS-1", HERMES_OLD)[0] == 503
        assert gateway.set_emergency_stop(HERMES_OLD) is False
        assert gateway.emergency_stopped is False
        assert gateway.set_emergency_stop(OPERATOR) is True
        assert gateway.emergency_stopped is True
        assert gateway.clear_emergency_stop(HERMES_OLD) is False
        assert gateway.clear_emergency_stop(OPERATOR) is True
    finally:
        gateway.stop()


def test_prompt_route_requires_exact_approved_execution_contract():
    gateway = make_gateway()
    gateway.start()
    try:
        base = {
            "decision_id": "decision-1",
            "proposal_hash": "a" * 64,
            "request_id": "request-1",
            "text": "hello",
        }
        assert request(gateway, "POST", "/v1/sessions/session-public-1/prompt",
                       HERMES_OLD, base)[0] == 404
        assert request(gateway, "POST", "/v1/sessions/session-public-1/prompt",
                       HERMES_OLD, {**base, "free": True})[0] == 400
        assert request(gateway, "POST", "/v1/sessions/session-public-1/prompt",
                       HERMES_OLD, {**base, "text": ""})[0] == 400
        assert request(gateway, "DELETE", "/v1/sessions/session-public-1", HERMES_OLD)[0] == 403
    finally:
        gateway.stop()


def test_decision_route_is_redacted_and_operator_token_cannot_read_it():
    gateway = make_gateway()
    gateway.start()
    proposal = proposal_payload()
    try:
        assert request(gateway, "POST", "/v1/proposals", HERMES_OLD, proposal)[0] == 201
        status, body = request(gateway, "GET", f"/v1/proposals/{proposal['decision_id']}", HERMES_OLD)
        assert status == 200
        assert body["decision"]["status"] == "awaiting_approval"
        assert "secret prompt" not in json.dumps(body)
        assert request(gateway, "GET", f"/v1/proposals/{proposal['decision_id']}", OPERATOR)[0] == 403
        assert request(gateway, "GET", "/v1/proposals/does-not-exist", HERMES_OLD)[0] == 404
    finally:
        gateway.stop()


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} agent gateway checks passed")
