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

from api.bridge import Bridge
from terminal import agent_decisions
from terminal import work_items
from terminal.agent_contracts import proposal_hash
from terminal.agent_gateway import AgentGateway
from terminal.input_protocol import prepare_composer_message
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


class LabeledSession(PromptSession):
    """Adds group_name, which the bare PromptSession fixture never needed before
    the Task 3 view model started reading it for session_label."""
    id = "abcd1234"
    group_name = "clones"
    session_key = "session-1"
    claude_session_id = "provider-1"


class RetryableSession(PromptSession):
    """A session that shows up under session-missing only after a retry --
    stands in for a tab reopening under the exact session_key an approved
    proposal targets."""
    id = "late-pty"
    session_key = "session-missing"
    claude_session_id = "provider-late"


def prompt_manager(session=None):
    manager = PtyManager.__new__(PtyManager)
    manager.sessions = {}
    manager.registry = SessionRegistry()
    session = session or PromptSession()
    manager.registry.register(session)
    return manager, session


def make_bridge(manager):
    bridge = Bridge.__new__(Bridge)
    bridge.pty_manager = manager
    bridge.ws_server = None
    bridge._operator_capabilities = {}
    bridge._capability_by_decision = {}
    bridge._hermes_publisher = None  # main.py injects the real one; None == never started
    return bridge


def approve_with_capability(bridge, decision_id, proposal_hash_value, reviewer_id="local-user"):
    token = bridge._issue_operator_capability(decision_id, proposal_hash_value)
    return bridge.approve_agent_proposal(decision_id, proposal_hash_value, reviewer_id, token)


def decisions_tmpdir():
    temp = tempfile.TemporaryDirectory()
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = temp.name
    return temp


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

        token = bridge._issue_operator_capability(proposal["decision_id"], proposal["proposal_hash"])
        second = bridge.retry_prompt_dispatch(proposal["decision_id"], proposal["proposal_hash"], token)

        assert second["state"] == "HOST_WRITE_ACCEPTED"
        assert second["request_id"] == first["request_id"]
        assert len(session.writes) == 1  # no second write happened
        assert agent_decisions.verify_audit_chain()
    finally:
        temp.cleanup()


def test_approve_agent_proposal_pre_write_failure_stays_retryable_and_then_retry_succeeds():
    temp = decisions_tmpdir()
    try:
        manager, _unused = prompt_manager()  # only registers "session-1"
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="retry me")
        proposal["action"]["target"]["session_key"] = "session-missing"
        proposal["proposal_hash"] = proposal_hash(proposal)
        agent_decisions.create_proposal(proposal)

        first = approve_with_capability(bridge, proposal["decision_id"], proposal["proposal_hash"])
        assert first["state"] == "WRITE_FAILED"
        assert first["retryable"] is True
        assert first["reason"] == "session_not_found"

        # Nothing was journaled: the decision is still approved and retryable.
        pending = agent_decisions.list_pending_decisions()
        entry = next(d for d in pending if d["decision_id"] == proposal["decision_id"])
        assert entry["pending_reason"] == "awaiting_dispatch"
        assert entry["execution"] is None

        # A retry without a valid capability must be refused.
        try:
            bridge.retry_prompt_dispatch(proposal["decision_id"], proposal["proposal_hash"], "stale-token")
        except PermissionError:
            pass
        else:
            raise AssertionError("retry without a valid capability must be refused")

        # The tab reopens under the exact session_key the approved proposal targets.
        late_session = RetryableSession()
        manager.registry.register(late_session)

        # The UI's own listing path is what actually hands out the fresh capability.
        pending = bridge.list_pending_agent_decisions()
        entry = next(d for d in pending if d["decision_id"] == proposal["decision_id"])
        assert entry["pending_reason"] == "awaiting_dispatch"
        token = entry["operator_capability"]

        second = bridge.retry_prompt_dispatch(proposal["decision_id"], proposal["proposal_hash"], token)
        assert second["state"] == "HOST_WRITE_ACCEPTED"
        assert second["request_id"] == first["request_id"]  # same uuid5, survives the retry
        assert late_session.writes
        assert agent_decisions.verify_audit_chain()
    finally:
        temp.cleanup()


def test_prompt_dispatch_reports_write_failed_when_emergency_stop_flips_after_approval():
    temp = decisions_tmpdir()
    try:
        manager, session = prompt_manager()
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="stopped mid-flight")
        agent_decisions.create_proposal(proposal)
        decision = agent_decisions.approve(
            proposal["decision_id"], proposal["proposal_hash"], "local-user", "cap-race")

        # Simulate the emergency stop flipping in the narrow window between
        # agent_decisions.approve() succeeding and the dispatch call that
        # follows it inside Bridge.approve_agent_proposal.
        agent_decisions.set_emergency_stop("tester", "cap-emergency")
        request_id = bridge._host_dispatch_request_id(proposal["decision_id"], proposal["proposal_hash"])
        receipt = bridge._prompt_dispatch_receipt(decision, request_id)

        assert receipt["state"] == "WRITE_FAILED"
        assert receipt["retryable"] is True
        assert receipt["reason"] == "emergency_stop_active"
        assert session.writes == []

        pending = agent_decisions.list_pending_decisions()
        entry = next(d for d in pending if d["decision_id"] == proposal["decision_id"])
        assert entry["pending_reason"] == "awaiting_dispatch"
        assert agent_decisions.verify_audit_chain()
    finally:
        agent_decisions.clear_emergency_stop("tester", "cap-emergency")
        temp.cleanup()


def test_reject_agent_proposal_never_dispatches_a_write():
    temp = decisions_tmpdir()
    try:
        manager, session = prompt_manager()
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="never sent")
        agent_decisions.create_proposal(proposal)
        token = bridge._issue_operator_capability(proposal["decision_id"], proposal["proposal_hash"])

        result = bridge.reject_agent_proposal(
            proposal["decision_id"], proposal["proposal_hash"], "local-user", token,
            "wrong_scope", "not now")

        assert result["status"] in {"replan_requested", "needs_clarification"}
        assert session.writes == []
    finally:
        temp.cleanup()


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


def test_gateway_prompt_route_is_host_owned_and_performs_no_write():
    # The HTTP prompt route is a dead end now (Task 2): even a fully valid,
    # approved decision gets the same fixed 403 and no PTY write, because
    # dispatch only ever happens from Bridge.approve_agent_proposal / retry.
    temp, gateway = setup_gateway()
    manager, session = prompt_manager()
    gateway.stop()
    gateway = AgentGateway(manager, Store(), Source(), Source(), port=0,
                           hermes_token=HERMES, operator_token=OPERATOR)
    gateway.start()
    try:
        proposal = make_proposal(action_type="send_prompt", text="will not be sent this way")
        assert request(gateway, "POST", "/v1/proposals", HERMES, proposal)[0] == 201
        agent_decisions.approve(proposal["decision_id"], proposal["proposal_hash"], "user", "cap-old-route")
        body = {"decision_id": proposal["decision_id"], "proposal_hash": proposal["proposal_hash"],
                "request_id": "prompt-old-route", "text": "will not be sent this way"}
        status, result = request(gateway, "POST", "/v1/sessions/session-1/prompt", HERMES, body)
        assert status == 403 and result["error"] == "prompt_dispatch_is_host_owned"
        assert session.writes == []
    finally:
        gateway.stop()
        temp.cleanup()


def test_list_pending_agent_decisions_returns_typed_view_model_with_session_and_work_item():
    temp = decisions_tmpdir()
    original_load_store = work_items.load_store
    try:
        manager, _session = prompt_manager(LabeledSession())
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="Faça um discovery com acentuação")
        agent_decisions.create_proposal(proposal)

        work_items.load_store = lambda: {
            "items": [{"id": "wi-1", "title": "Contrato em ativação"}],
            "session_links": {"session-1": {"wi_id": "wi-1"}},
        }
        items = bridge.list_pending_agent_decisions()

        assert len(items) == 1
        item = items[0]
        assert item["decision_id"] == proposal["decision_id"]
        assert item["proposal_hash"] == proposal["proposal_hash"]
        assert item["pending_reason"] == "awaiting_approval"
        assert item["prompt_text"] == "Faça um discovery com acentuação"
        assert item["prompt_length"] == len("Faça um discovery com acentuação")
        assert item["intent"] == proposal["intent"]
        assert item["expected_outcome"] == proposal["action"]["expected_outcome"]
        assert item["risk_level"] == "low"
        assert item["version"] == 1
        assert item["parent_decision_id"] is None
        assert item["trace_id"] == proposal["trace_id"]
        assert item["session_key"] == "session-1"
        assert item["session_label"] == "clones #abcd"
        assert item["work_item_id"] == "wi-1"
        assert item["work_item_title"] == "Contrato em ativação"
        assert item["operator_capability"]
        assert item["raw"]["decision_id"] == proposal["decision_id"]
    finally:
        work_items.load_store = original_load_store
        temp.cleanup()


def test_list_pending_agent_decisions_falls_back_to_claude_session_id_for_work_item_link():
    # session_links is keyed by claude_session_id for claude tabs (app.js
    # sessionLinkKeyOf); a proposal only ever carries the stable session_key, so
    # the bridge must try the live session's claude_session_id as a fallback.
    temp = decisions_tmpdir()
    original_load_store = work_items.load_store
    try:
        manager, _session = prompt_manager(LabeledSession())
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="fallback path")
        agent_decisions.create_proposal(proposal)

        work_items.load_store = lambda: {
            "items": [{"id": "wi-2", "title": "Linked via provider id"}],
            "session_links": {"provider-1": {"wi_id": "wi-2"}},
        }
        item = bridge.list_pending_agent_decisions()[0]
        assert item["work_item_id"] == "wi-2"
        assert item["work_item_title"] == "Linked via provider id"
    finally:
        work_items.load_store = original_load_store
        temp.cleanup()


def test_list_pending_agent_decisions_degrades_defensively_when_tab_and_store_are_gone():
    # A closed tab (find_by_session_key returns None) plus a missing link or a
    # corrupt store must degrade to null/"" and never raise -- the decision is
    # still worth showing even with no session/work-item context left to join.
    # An ARCHIVED link is NOT "missing": set_waiting() archives every link of a
    # waiting item, and blanking the work item there contradicted the Work Item
    # panel, which joins without that sensitivity.
    temp = decisions_tmpdir()
    original_load_store = work_items.load_store
    try:
        manager, _session = prompt_manager()  # registers "session-1", not the target below
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="orphaned")
        proposal["action"]["target"]["session_key"] = "session-closed"
        proposal["proposal_hash"] = proposal_hash(proposal)
        agent_decisions.create_proposal(proposal)

        # Archived link, item still in the store: id AND title survive.
        work_items.load_store = lambda: {
            "items": [{"id": "wi-3", "title": "Item em espera"}],
            "session_links": {"session-closed": {"wi_id": "wi-3", "archived": True}},
        }
        item = bridge.list_pending_agent_decisions()[0]
        assert item["session_label"] is None  # tab is closed, nothing to label
        assert item["work_item_id"] == "wi-3"
        assert item["work_item_title"] == "Item em espera"

        # No link at all for this session: degrade to null/"".
        work_items.load_store = lambda: {"items": [], "session_links": {}}
        item = bridge.list_pending_agent_decisions()[0]
        assert item["session_label"] is None
        assert item["work_item_id"] is None
        assert item["work_item_title"] == ""

        work_items.load_store = lambda: (_ for _ in ()).throw(RuntimeError("store is corrupt"))
        item = bridge.list_pending_agent_decisions()[0]
        assert item["work_item_id"] is None
        assert item["work_item_title"] == ""
    finally:
        work_items.load_store = original_load_store
        temp.cleanup()


def test_verify_proposal_hash_confirms_match_and_reports_prompt_length():
    temp = decisions_tmpdir()
    try:
        manager, _session = prompt_manager()
        bridge = make_bridge(manager)
        text = "Confere byte a byte: ação, coração,ções."
        proposal = make_proposal(action_type="send_prompt", text=text)
        agent_decisions.create_proposal(proposal)

        result = bridge.verify_proposal_hash(proposal["decision_id"], rendered_text=text)
        assert result == {
            "found": True, "hash_ok": True, "text_ok": True,
            "prompt_length": len(text), "proposal_hash": proposal["proposal_hash"],
        }
        # No rendered_text supplied: text_ok stays None rather than False.
        no_text = bridge.verify_proposal_hash(proposal["decision_id"])
        assert no_text["hash_ok"] is True and no_text["text_ok"] is None
    finally:
        temp.cleanup()


def test_verify_proposal_hash_flags_a_recorded_hash_that_does_not_match_the_proposal():
    # validate_proposal refuses a mismatched hash outright, so this state cannot
    # be reached through create_proposal -- monkeypatch get_decision instead
    # (same technique as the record_automatic_action failure injection above).
    temp = decisions_tmpdir()
    original_get_decision = agent_decisions.get_decision
    try:
        manager, _session = prompt_manager()
        bridge = make_bridge(manager)
        proposal = make_proposal(action_type="send_prompt", text="tampered")
        tampered_decision = {
            "proposal": proposal,
            "proposal_hash": "0" * 64,  # does not match proposal_hash(proposal)
        }
        agent_decisions.get_decision = lambda decision_id: (
            tampered_decision if decision_id == "d-tampered" else None)

        result = bridge.verify_proposal_hash("d-tampered", rendered_text="different rendered text")
        assert result["found"] is True
        assert result["hash_ok"] is False
        assert result["text_ok"] is False
        assert result["prompt_length"] == len("tampered")
        assert result["proposal_hash"] == "0" * 64
    finally:
        agent_decisions.get_decision = original_get_decision
        temp.cleanup()


def test_verify_proposal_hash_not_found_is_distinct_from_a_hash_mismatch():
    temp = decisions_tmpdir()
    try:
        bridge = make_bridge(prompt_manager()[0])
        result = bridge.verify_proposal_hash("does-not-exist")
        assert result == {
            "found": False, "hash_ok": False, "text_ok": None,
            "prompt_length": 0, "proposal_hash": None,
        }
    finally:
        temp.cleanup()


def test_operator_capability_reissue_discards_the_previous_token_and_stays_bounded():
    bridge = make_bridge(Pty())
    decision_id, proposal_hash_value = "decision-x", "h" * 64

    tokens = [bridge._issue_operator_capability(decision_id, proposal_hash_value) for _ in range(50)]

    # Only the latest token for this decision survives -- the actual bound on
    # steady-state growth, not just the expiry sweep.
    assert len(bridge._operator_capabilities) == 1
    assert bridge._capability_by_decision[decision_id] == tokens[-1]
    assert not bridge._consume_operator_capability(tokens[0], decision_id, proposal_hash_value)
    assert bridge._consume_operator_capability(tokens[-1], decision_id, proposal_hash_value)
    # Consumed exactly once: gone from both maps, not merely flagged used.
    assert bridge._operator_capabilities == {}
    assert decision_id not in bridge._capability_by_decision


def test_operator_capability_purge_evicts_stale_entries_left_by_decisions_no_longer_polled():
    bridge = make_bridge(Pty())
    stale_token = bridge._issue_operator_capability("decision-old", "h" * 64)
    bridge._operator_capabilities[stale_token]["expires_at"] = time.time() - 1

    # Issuing for an unrelated decision must sweep the stale one too, not just
    # bound the decision it was issued for.
    bridge._issue_operator_capability("decision-new", "h" * 64)

    assert stale_token not in bridge._operator_capabilities
    assert "decision-old" not in bridge._capability_by_decision


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} approval flow checks passed")
