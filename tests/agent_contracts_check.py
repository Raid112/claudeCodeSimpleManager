import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.agent_contracts import (
    ACTION_OPEN_SESSION,
    ACTION_SEND_PROMPT,
    ACTION_LINK_WORK_ITEM,
    EXECUTION_ACCEPTED,
    EXECUTION_UNKNOWN,
    MODEL_NAME,
    MODEL_PROVIDER,
    STATUS_AWAITING_APPROVAL,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    can_transition,
    proposal_hash,
    validate_feedback,
    validate_idempotency,
    validate_model_attestation,
    validate_proposal,
)


def now_iso(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def valid_attestation():
    return {
        "hermes_profile": "claudemanager",
        "hermes_version": "0.19.0",
        "config_hash": "a" * 64,
        "session_id": "hermes-session-1",
    }


def valid_proposal():
    payload = {
        "decision_id": "11111111-1111-4111-8111-111111111111",
        "parent_decision_id": None,
        "trace_id": "22222222-2222-4222-8222-222222222222",
        "version": 1,
        "context_refs": [{
            "source": "jira",
            "object_id": "DS-123",
            "retrieved_at": now_iso(),
            "content_hash": "b" * 64,
            "trust": "external_data",
        }],
        "intent": "Open the selected project session",
        "action": {
            "type": ACTION_OPEN_SESSION,
            "target": {"group_id": "group-1"},
            "parameters": {},
            "expected_outcome": "A managed session is available",
            "reversible": True,
        },
        "risk": {"level": "low", "factors": []},
        "policy": {
            "version": "1",
            "decision": "approval_required",
            "constraints": [],
        },
        "model": {
            "provider": MODEL_PROVIDER,
            "name": MODEL_NAME,
            "prompt_version": "v1",
            "attestation": valid_attestation(),
        },
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "created_at": now_iso(),
        "expires_at": now_iso(300),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    payload["status"] = STATUS_PROPOSED
    return payload


def assert_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def test_valid_proposal_is_deterministic():
    proposal = valid_proposal()
    ok, errors = validate_proposal(proposal)
    assert ok, errors
    assert proposal_hash(proposal) == proposal["proposal_hash"]
    assert proposal_hash(copy.deepcopy(proposal)) == proposal_hash(proposal)
    assert EXECUTION_ACCEPTED == "ACCEPTED"
    assert EXECUTION_UNKNOWN == "UNKNOWN"


def test_missing_and_unknown_security_fields_are_rejected():
    proposal = valid_proposal()
    proposal.pop("intent")
    ok, errors = validate_proposal(proposal)
    assert not ok and any("intent" in error for error in errors)

    proposal = valid_proposal()
    proposal["unexpected"] = True
    ok, errors = validate_proposal(proposal)
    assert not ok and any("unexpected" in error for error in errors)


def test_deny_always_and_unknown_actions_never_validate():
    for action_type in ("shell", "powershell", "jira_transition", "delete", "deploy", "unknown"):
        proposal = valid_proposal()
        proposal["action"]["type"] = action_type
        proposal["proposal_hash"] = proposal_hash(proposal)
        ok, errors = validate_proposal(proposal)
        assert not ok, action_type
        assert any("action" in error for error in errors)


def test_model_attestation_must_be_verified_luna():
    proposal = valid_proposal()
    proposal["model"]["name"] = "auto"
    proposal["proposal_hash"] = proposal_hash(proposal)
    ok, errors = validate_proposal(proposal)
    assert not ok and any("model" in error for error in errors)

    bad = valid_attestation()
    bad["config_hash"] = "not-a-hash"
    assert_raises(ValueError, lambda: validate_model_attestation(bad))


def test_expired_proposal_and_changed_hash_are_rejected():
    proposal = valid_proposal()
    proposal["expires_at"] = now_iso(-1)
    proposal["proposal_hash"] = proposal_hash(proposal)
    ok, errors = validate_proposal(proposal)
    assert not ok and any("expired" in error for error in errors)

    proposal = valid_proposal()
    proposal["intent"] = "changed after hashing"
    ok, errors = validate_proposal(proposal)
    assert not ok and any("hash" in error for error in errors)


def test_idempotency_and_feedback_scope_are_validated():
    assert_raises(ValueError, lambda: validate_idempotency("bad", "a" * 64))
    assert_raises(ValueError, lambda: validate_idempotency(
        "33333333-3333-4333-8333-333333333333", "bad"))

    feedback = {
        "feedback_id": "44444444-4444-4444-8444-444444444444",
        "decision_id": "11111111-1111-4111-8111-111111111111",
        "reviewer_id": "local-user",
        "verdict": "reject",
        "reason_code": "wrong_scope",
        "comment": "Use the other project",
        "scope": "this_proposal",
        "requested_changes": ["target group-2"],
        "created_at": now_iso(),
    }
    ok, errors = validate_feedback(feedback)
    assert ok, errors
    feedback["scope"] = "global_policy"
    ok, errors = validate_feedback(feedback)
    assert not ok and any("scope" in error for error in errors)


def test_state_transitions_are_explicit():
    assert can_transition(STATUS_PROPOSED, STATUS_AWAITING_APPROVAL)
    assert can_transition(STATUS_AWAITING_APPROVAL, STATUS_REJECTED)
    assert not can_transition(STATUS_REJECTED, STATUS_AWAITING_APPROVAL)


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} agent contract checks passed")
