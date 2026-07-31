import copy
import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.agent_contracts import proposal_hash
from terminal import agent_decisions


def now_iso(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def proposal(version=1, parent=None, idem=None):
    payload = {
        "decision_id": str(uuid.uuid4()),
        "parent_decision_id": parent,
        "trace_id": str(uuid.uuid4()),
        "version": version,
        "context_refs": [{
            "source": "todo",
            "object_id": "todo-1",
            "retrieved_at": now_iso(),
            "content_hash": "b" * 64,
            "trust": "external_data",
        }],
        "intent": "Open a managed session",
        "action": {
            "type": "open_session",
            "target": {"group_id": "group-1"},
            "parameters": {},
            "expected_outcome": "Session opened",
            "reversible": True,
        },
        "risk": {"level": "low", "factors": []},
        "policy": {"version": "1", "decision": "approval_required", "constraints": []},
        "model": {
            "provider": "openai-codex",
            "name": "gpt-5.6-luna",
            "prompt_version": "v1",
            "attestation": {
                "hermes_profile": "test",
                "hermes_version": "0.19.0",
                "config_hash": "a" * 64,
                "session_id": "hermes-test",
            },
        },
        "idempotency_key": idem or str(uuid.uuid4()),
        "created_at": now_iso(),
        "expires_at": now_iso(300),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    return payload


def feedback(decision_id, verdict="reject", feedback_id=None, comment="Use another group"):
    return {
        "feedback_id": feedback_id or str(uuid.uuid4()),
        "decision_id": decision_id,
        "reviewer_id": "local-user",
        "verdict": verdict,
        "reason_code": "wrong_scope",
        "comment": comment,
        "scope": "this_proposal",
        "requested_changes": ["Use group-2"],
        "created_at": now_iso(),
    }


def setup():
    root = tempfile.TemporaryDirectory()
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = root.name
    return root


def test_append_only_chain_and_reconstruction():
    root = setup()
    try:
        p = proposal()
        created = agent_decisions.create_proposal(p)
        assert created["status"] == "awaiting_approval"
        assert agent_decisions.get_decision(p["decision_id"])["proposal_hash"] == p["proposal_hash"]
        assert agent_decisions.verify_audit_chain()
        files = list(Path(root.name).glob("*.jsonl"))
        assert files
        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["previous_hash"] is None
        assert event["event_hash"]
        assert event["payload_hash"] == p["proposal_hash"]
    finally:
        root.cleanup()


def test_tampering_and_reordering_block_reads():
    root = setup()
    try:
        p = proposal()
        agent_decisions.create_proposal(p)
        path = next(Path(root.name).glob("*.jsonl"))
        event = json.loads(path.read_text(encoding="utf-8"))
        event["actor"] = "tampered"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        try:
            agent_decisions.get_decision(p["decision_id"])
        except agent_decisions.AuditIntegrityError:
            pass
        else:
            raise AssertionError("tampered audit must fail closed")
    finally:
        root.cleanup()


def test_duplicate_rejection_is_idempotent_and_concurrent():
    root = setup()
    try:
        p = proposal()
        agent_decisions.create_proposal(p)
        f = feedback(p["decision_id"], feedback_id="55555555-5555-4555-8555-555555555555")
        results = []
        errors = []

        def reject():
            try:
                results.append(agent_decisions.record_feedback(p["decision_id"], copy.deepcopy(f)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reject) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        request_ids = {result["replan_request_id"] for result in results}
        assert len(request_ids) == 1
        decision = agent_decisions.get_decision(p["decision_id"])
        assert decision["status"] == "replan_requested"
        assert len(decision["feedback"]) == 1
        assert decision["attempt"] == 1
    finally:
        root.cleanup()


def test_idempotency_conflict_lineage_and_unknown_result():
    root = setup()
    try:
        key = str(uuid.uuid4())
        p = proposal(idem=key)
        first = agent_decisions.create_proposal(p)
        assert agent_decisions.create_proposal(copy.deepcopy(p))["decision_id"] == first["decision_id"]
        other = proposal(idem=key)
        try:
            agent_decisions.create_proposal(other)
        except agent_decisions.ConflictError:
            pass
        else:
            raise AssertionError("same idempotency key with changed payload must conflict")

        agent_decisions.approve(p["decision_id"], p["proposal_hash"], "local-user", "operator-cap")
        accepted = agent_decisions.execute_approved(p["decision_id"], p["proposal_hash"], "request-1")
        assert accepted["state"] == "ACCEPTED"
        unknown = agent_decisions.append_execution_result(
            p["decision_id"], {"request_id": "request-1", "state": "UNKNOWN", "reason": "write uncertain"})
        assert unknown["status"] == "unknown"
    finally:
        root.cleanup()


def test_replan_attempts_are_bounded_and_parent_cannot_approve_child():
    root = setup()
    try:
        p = proposal()
        agent_decisions.create_proposal(p)
        for attempt in range(3):
            f = feedback(p["decision_id"], feedback_id=str(uuid.uuid4()))
            result = agent_decisions.record_feedback(p["decision_id"], f)
            if attempt < 2:
                assert result["replan_request_id"]
                p = proposal(version=attempt + 2, parent=p["decision_id"])
                agent_decisions.create_proposal(p)
        child = agent_decisions.get_decision(p["decision_id"])
        assert child["status"] == "replan_requested"
        assert child["parent_decision_id"]
        try:
            agent_decisions.approve(child["decision_id"], child["proposal_hash"], "local-user", "operator-cap")
        except agent_decisions.AuthorizationError:
            pass
        else:
            raise AssertionError("rejected child must not be approved")
    finally:
        root.cleanup()


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} agent decision checks passed")
