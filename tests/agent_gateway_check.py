import http.client
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.agent_gateway import AgentGateway, EVENTS_MAX_RECORDS
from terminal.agent_contracts import proposal_hash
from terminal import agent_decisions

HERMES_OLD = "hermes-old-" + "a" * 32
HERMES_NEW = "hermes-new-" + "b" * 32
OPERATOR = "operator-" + "c" * 32
_DECISION_ROOTS = []


class FakeSession:
    def __init__(self, session_key, terminal_type="claude", claude_session_id=None,
                 agent_session_id=None, group_name="Project", path=r"C:\secret\project"):
        self.session_key = session_key
        self.terminal_type = terminal_type
        self.claude_session_id = claude_session_id
        self.agent_session_id = agent_session_id
        self.group_name = group_name
        self.path = path


class FakePtyManager:
    def __init__(self):
        self.prompts = []
        self.owned_prompts = []
        self.by_key = {
            "session-public-1": FakeSession(
                "session-public-1", terminal_type="claude", claude_session_id="private-provider-id"),
        }

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

    def send_owned_prompt(self, session_key, text):
        self.owned_prompts.append((session_key, text))
        return True

    def find_by_session_key(self, session_key):
        return self.by_key.get(session_key)


class FakeWorkItems:
    def __init__(self):
        self.links = []
        self.events = []

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

    def link(self, link_key, work_item_id, group_name=None, path=None):
        self.links.append((link_key, work_item_id, group_name, path))

    def read_events(self, since=None, until=None):
        return [e for e in self.events
                if (since is None or e.get("ts", 0) >= since)
                and (until is None or e.get("ts", 0) <= until)]


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


PROMPT_PATH = "/v1/sessions/session-public-1/prompt"


def _record_hermes_open(session_key="session-public-1"):
    agent_decisions.record_automatic_action(
        "open_session", "open-" + session_key, {"session_key": session_key, "state": "DISPATCHED"})


def test_prompt_route_stays_closed_without_allow_listed_group():
    # Default config: no auto_prompt_groups -> the lane is closed even for a
    # Hermes-opened tab, and the old approval contract is not accepted here.
    gateway = make_gateway(groups=[{"name": "Project", "path": "C:/x"}])
    gateway.start()
    try:
        _record_hermes_open()
        status, health = request(gateway, "GET", "/v1/health", HERMES_OLD)
        assert health["read_only"] is True and health["auto_prompt_groups"] == []
        status, response = request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                                   {"request_id": "r-1", "text": "oi"})
        assert status == 403 and response["error"] == "group_not_enabled_for_auto_prompt"
        status, response = request(gateway, "POST", PROMPT_PATH, HERMES_OLD, {
            "decision_id": "d", "proposal_hash": "h", "request_id": "r-2", "text": "oi"})
        assert status == 400 and response["error"] == "owned_prompt_contract"
        assert gateway.pty_manager.owned_prompts == []
        assert request(gateway, "DELETE", "/v1/sessions/session-public-1", HERMES_OLD)[0] == 403
    finally:
        gateway.stop()


def test_owned_prompt_requires_hermes_opened_tab_and_is_idempotent():
    gateway = make_gateway(groups=[{"name": "Project", "path": "C:/x"}],
                           auto_prompt_groups=["Project", "not-configured"])
    gateway.start()
    try:
        assert gateway.auto_prompt_groups == {"Project"}
        # Tab the user opened by hand: no open_session in the journal.
        status, response = request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                                   {"request_id": "r-1", "text": "oi"})
        assert status == 403 and response["error"] == "session_not_opened_by_hermes"
        assert gateway.pty_manager.owned_prompts == []

        _record_hermes_open()
        assert request(gateway, "POST", PROMPT_PATH, OPERATOR,
                       {"request_id": "r-2", "text": "oi"})[0] == 403
        status, response = request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                                   {"request_id": "r-3", "text": "analisa o DS-1"})
        assert status == 201 and response["prompt"]["state"] == "SENT"
        # Replay with the same request_id returns the record, no second write.
        status, again = request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                                {"request_id": "r-3", "text": "analisa o DS-1"})
        assert status == 201 and again["prompt"] == response["prompt"]
        assert gateway.pty_manager.owned_prompts == [("session-public-1", "analisa o DS-1")]

        assert request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                       {"request_id": "r-4", "text": "   "})[0] == 400
        assert request(gateway, "POST", "/v1/sessions/missing/prompt", HERMES_OLD,
                       {"request_id": "r-5", "text": "oi"})[0] == 404

        assert gateway.set_emergency_stop(OPERATOR) is True
        assert request(gateway, "POST", PROMPT_PATH, HERMES_OLD,
                       {"request_id": "r-6", "text": "oi"})[0] == 503
        assert gateway.clear_emergency_stop(OPERATOR) is True
        status, health = request(gateway, "GET", "/v1/health", HERMES_OLD)
        assert health["read_only"] is False and health["auto_prompt_groups"] == ["Project"]
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


def test_link_work_item_happy_path_replay_and_wrong_action_conflict():
    gateway = make_gateway()
    gateway.start()
    try:
        body = {"request_id": "link-req-1", "session_key": "session-public-1", "work_item_id": "wi_1"}
        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, body)
        assert status == 201, resp
        # claude tab -> keyed by claude_session_id (app.js sessionLinkKeyOf), not session_key.
        assert resp["link"]["link_key"] == "private-provider-id"
        assert gateway.work_item_store.links == [
            ("private-provider-id", "wi_1", "Project", r"C:\secret\project")]

        # replay of the same request_id: same 201, same body, no second link() call.
        status2, resp2 = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, body)
        assert status2 == 201 and resp2 == resp
        assert len(gateway.work_item_store.links) == 1

        # request_id already bound to a DIFFERENT automatic action -> 409, no link recorded.
        agent_decisions.record_automatic_action("open_session", "shared-req", {"ok": True})
        conflict_body = {"request_id": "shared-req", "session_key": "session-public-1",
                          "work_item_id": "wi_1"}
        status3, resp3 = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, conflict_body)
        assert status3 == 409, resp3
        assert len(gateway.work_item_store.links) == 1
    finally:
        gateway.stop()


def test_link_work_item_validation_and_not_found():
    gateway = make_gateway()
    gateway.start()
    try:
        base = {"request_id": "req-nf-1", "session_key": "session-public-1", "work_item_id": "wi_1"}
        for bad in ({**base, "extra": "x"}, {**base, "work_item_id": ""}, {"anything": True}):
            status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, bad)
            assert status == 400, (bad, status, resp)

        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD,
                               {**base, "request_id": "req-nf-2", "session_key": "no-such-session"})
        assert status == 404 and resp["error"] == "session_not_found"

        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD,
                               {**base, "request_id": "req-nf-3", "work_item_id": "wi_missing"})
        assert status == 404 and resp["error"] == "work_item_not_found"
        assert gateway.work_item_store.links == []
    finally:
        gateway.stop()


def test_link_work_item_keys_non_claude_by_session_key_and_denies_powershell():
    gateway = make_gateway()
    gateway.pty_manager.by_key["codex-session"] = FakeSession(
        "codex-session", terminal_type="codex", agent_session_id="agent-xyz")
    gateway.pty_manager.by_key["ps-session"] = FakeSession("ps-session", terminal_type="powershell")
    gateway.start()
    try:
        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, {
            "request_id": "req-codex-1", "session_key": "codex-session", "work_item_id": "wi_1"})
        assert status == 201, resp
        assert resp["link"]["link_key"] == "codex-session"

        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, {
            "request_id": "req-ps-1", "session_key": "ps-session", "work_item_id": "wi_1"})
        assert status == 400 and resp["error"] == "session_not_linkable"
    finally:
        gateway.stop()


def test_link_work_item_requires_hermes_token_and_respects_emergency_stop():
    gateway = make_gateway()
    gateway.start()
    try:
        body = {"request_id": "req-stop-1", "session_key": "session-public-1", "work_item_id": "wi_1"}
        assert request(gateway, "POST", "/v1/work-items/link", None, body)[0] == 401
        assert request(gateway, "POST", "/v1/work-items/link", OPERATOR, body)[0] == 403

        assert gateway.set_emergency_stop(OPERATOR) is True
        status, resp = request(gateway, "POST", "/v1/work-items/link", HERMES_OLD, body)
        assert status == 503 and resp["error"] == "emergency_stop_active"
        assert gateway.work_item_store.links == []
    finally:
        gateway.stop()


def test_events_route_normalizes_disambiguates_and_validates_since():
    gateway = make_gateway()
    gateway.start()
    try:
        now = time.time()
        gateway.work_item_store.events = [
            {"ts": now - 5, "session_key": "skey-1", "event": "PreToolUse",
             "tool": "Bash", "claude_session_id": None},
            {"ts": now - 4, "session_key": "skey-1", "event": "Notification",
             "tool": None, "claude_session_id": None},
            {"ts": now - 3, "session_key": "skey-1", "event": "Stop",
             "tool": None, "claude_session_id": None},
            # session_key is None (pre-rekey/unmanaged record): normalize_event
            # never publishes it, regardless of the raw event name.
            {"ts": now - 2, "session_key": None, "event": "Stop",
             "tool": None, "claude_session_id": "sidX"},
        ]
        status, body = request(gateway, "GET", f"/v1/events?since={now - 10}", HERMES_OLD)
        assert status == 200, body
        events = body["events"]
        # PreToolUse is logged-only; the Notification right after it disambiguates
        # to permission_requested (needs the PreToolUse context); Stop is always
        # task_completed; the session_key=None Stop never surfaces.
        assert {e["event_type"] for e in events} == {"permission_requested", "task_completed"}
        assert all(e.get("work_item_id") == "" for e in events)  # no session_links match
        assert body["truncated"] is False
        assert body["cap"] == EVENTS_MAX_RECORDS
        assert body["next_since"] >= now - 2

        status, body = request(gateway, "GET", "/v1/events?since=not-a-number", HERMES_OLD)
        assert status == 400 and body["error"] == "invalid_since"
        status, body = request(gateway, "GET", "/v1/events?since=-5", HERMES_OLD)
        assert status == 400 and body["error"] == "invalid_since"

        # missing `since` must not replay the whole (unbounded) log -- still 200,
        # bounded to the default recent window, not an error.
        status, body = request(gateway, "GET", "/v1/events", HERMES_OLD)
        assert status == 200
    finally:
        gateway.stop()


def test_events_route_caps_response_and_next_since_resumes_at_the_cut():
    gateway = make_gateway()
    gateway.start()
    try:
        now = time.time()
        n = EVENTS_MAX_RECORDS + 10
        gateway.work_item_store.events = [
            {"ts": now - n + i, "session_key": f"skey-{i}", "event": "Stop",
             "tool": None, "claude_session_id": None}
            for i in range(n)
        ]
        status, body = request(gateway, "GET", f"/v1/events?since={now - n - 5}", HERMES_OLD)
        assert status == 200
        assert body["truncated"] is True
        assert len(body["events"]) == EVENTS_MAX_RECORDS
        expected_next_since = now - n + (EVENTS_MAX_RECORDS - 1)
        assert abs(body["next_since"] - expected_next_since) < 0.01, body["next_since"]
    finally:
        gateway.stop()


def test_events_route_quiet_window_advances_next_since_past_logged_only_noise():
    gateway = make_gateway()
    gateway.start()
    try:
        now = time.time()
        # only logged-only records in the window: nothing publishable, but the
        # cursor must still advance past them instead of re-reading forever.
        gateway.work_item_store.events = [
            {"ts": now - 3, "session_key": "skey-1", "event": "PreToolUse",
             "tool": "Bash", "claude_session_id": None},
            {"ts": now - 1, "session_key": "skey-1", "event": "PostToolUse",
             "tool": "Bash", "claude_session_id": None},
        ]
        status, body = request(gateway, "GET", f"/v1/events?since={now - 10}", HERMES_OLD)
        assert status == 200
        assert body["events"] == []
        assert body["truncated"] is False
        assert body["next_since"] >= now - 1

        # a genuinely quiet window (no records at all) advances to "now", not to
        # the old `since`, so the caller's window keeps sliding forward.
        gateway.work_item_store.events = []
        status, body = request(gateway, "GET", f"/v1/events?since={now - 10}", HERMES_OLD)
        assert status == 200
        assert body["next_since"] > now - 10
    finally:
        gateway.stop()


def test_events_route_requires_hermes_token_and_ignores_emergency_stop():
    gateway = make_gateway()
    gateway.start()
    try:
        assert request(gateway, "GET", "/v1/events", None)[0] == 401
        assert request(gateway, "GET", "/v1/events", OPERATOR)[0] == 403
        assert request(gateway, "GET", "/v1/events", HERMES_OLD)[0] == 200

        assert gateway.set_emergency_stop(OPERATOR) is True
        status, _ = request(gateway, "GET", "/v1/events", HERMES_OLD)
        assert status == 200  # a read of already-logged metadata is not gated by the stop
    finally:
        gateway.stop()


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} agent gateway checks passed")
