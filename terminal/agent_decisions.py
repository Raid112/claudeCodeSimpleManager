"""Append-only decision, approval, execution, and replan journal.

The journal is deliberately independent from ``work_items.json`` and ``sessions.json``.
Every mutation is an event in a hash chain.  Reads reconstruct state from that chain so a
crash cannot leave a mutable snapshot half-written or silently erase a rejection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from terminal.agent_contracts import (
    EXECUTION_ACCEPTED,
    EXECUTION_DISPATCHED,
    EXECUTION_SENT,
    EXECUTION_UNKNOWN,
    STATUS_APPROVED,
    STATUS_AWAITING_APPROVAL,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_REJECTED,
    STATUS_REPLAN_REQUESTED,
    STATUS_UNKNOWN,
    can_transition,
    proposal_hash,
    validate_feedback,
    validate_proposal,
)
from terminal.hook_state import get_data_dir


MAX_REPLAN_ATTEMPTS = 3


class DecisionError(RuntimeError):
    pass


class ConflictError(DecisionError):
    pass


class AuthorizationError(DecisionError):
    pass


class StateError(DecisionError):
    pass


class AuditIntegrityError(DecisionError):
    pass


class AuditStorageError(DecisionError):
    pass


_PROCESS_LOCK = threading.RLock()


def _root() -> Path:
    configured = os.environ.get("CLAUDEMANAGER_AGENT_DECISIONS_DIR")
    root = Path(configured) if configured else get_data_dir() / "agent-decisions"
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError as exc:
        raise AuditStorageError(f"cannot secure decision journal: {exc}") from exc
    return root


def _lock_path() -> Path:
    return _root() / ".lock"


class _FileLock:
    def __enter__(self):
        self._handle = open(_lock_path(), "a+b")
        self._handle.seek(0)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _payload_digest(payload: Any) -> str:
    if isinstance(payload, dict) and "proposal_hash" in payload:
        return proposal_hash(payload)
    return _digest(payload)


def _event_files() -> list[Path]:
    return sorted(_root().glob("events-*.jsonl"))


def _read_events_unlocked() -> list[dict]:
    events: list[dict] = []
    for path in _event_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AuditIntegrityError(f"cannot read audit partition: {exc}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise AuditIntegrityError(f"blank audit line at {path}:{line_number}")
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AuditIntegrityError(f"invalid audit JSON at {path}:{line_number}") from exc
    events.sort(key=lambda event: event.get("sequence", -1))
    previous_hash = None
    for expected_sequence, event in enumerate(events, 1):
        required = {
            "sequence", "ts", "kind", "actor", "role", "capability_id", "previous_hash",
            "event_hash", "payload_hash", "policy_version", "execution_id", "payload",
        }
        if not required.issubset(event):
            raise AuditIntegrityError("audit event is missing required fields")
        if event["sequence"] != expected_sequence:
            raise AuditIntegrityError("audit sequence is missing, duplicated, or reordered")
        if event["previous_hash"] != previous_hash:
            raise AuditIntegrityError("audit previous hash does not match")
        if event["payload_hash"] != _payload_digest(event["payload"]):
            raise AuditIntegrityError("audit payload hash does not match")
        candidate = dict(event)
        actual_hash = candidate.pop("event_hash")
        if actual_hash != _digest(candidate):
            raise AuditIntegrityError("audit event hash does not match")
        previous_hash = actual_hash
    return events


def verify_audit_chain() -> bool:
    with _PROCESS_LOCK, _FileLock():
        _read_events_unlocked()
    return True


def _append_event_unlocked(kind: str, payload: dict, *, actor: str = "claudemanager",
                           role: str = "host", capability: str | None = None,
                           policy_version: str = "1", execution_id: str | None = None) -> dict:
    events = _read_events_unlocked()
    sequence = len(events) + 1
    previous_hash = events[-1]["event_hash"] if events else None
    event = {
        "sequence": sequence,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "actor": actor,
        "role": role,
        "capability_id": _digest(capability) if capability else None,
        "previous_hash": previous_hash,
        "payload_hash": _payload_digest(payload),
        "policy_version": policy_version,
        "execution_id": execution_id,
        "payload": copy.deepcopy(payload),
    }
    event["event_hash"] = _digest(event)
    path = _root() / f"events-{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise AuditStorageError(f"cannot append audit event: {exc}") from exc
    return event


def _append_event(*args, **kwargs) -> dict:
    with _PROCESS_LOCK, _FileLock():
        return _append_event_unlocked(*args, **kwargs)


def _states_from_events(events: list[dict]) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for event in events:
        payload = event["payload"]
        kind = event["kind"]
        if kind == "proposal_created":
            proposal = copy.deepcopy(payload)
            states[proposal["decision_id"]] = {
                "decision_id": proposal["decision_id"],
                "parent_decision_id": proposal.get("parent_decision_id"),
                "proposal_hash": proposal["proposal_hash"],
                "version": proposal["version"],
                "proposal": proposal,
                "status": STATUS_AWAITING_APPROVAL,
                "feedback": [],
                "attempt": 0,
                "replan_request_id": None,
                "approval": None,
                "execution": None,
                "created_at": proposal["created_at"],
                "expires_at": proposal["expires_at"],
            }
        elif not isinstance(payload, dict) or payload.get("decision_id") not in states:
            continue
        else:
            state = states[payload["decision_id"]]
            if kind == "feedback_recorded":
                state["feedback"].append(copy.deepcopy(payload["feedback"]))
            elif kind == "rejection_transaction":
                state["feedback"].append(copy.deepcopy(payload["feedback"]))
                state["attempt"] = payload["attempt"]
                state["replan_request_id"] = payload.get("replan_request_id")
                state["status"] = payload["status"]
            elif kind == "approval_recorded":
                state["approval"] = copy.deepcopy(payload)
                state["status"] = STATUS_APPROVED
            elif kind == "execution_accepted":
                state["execution"] = copy.deepcopy(payload)
            elif kind == "execution_result":
                state["execution"] = copy.deepcopy(payload)
                state["status"] = payload.get("status", state["status"])
    return states


def _all_states_unlocked() -> dict[str, dict]:
    return _states_from_events(_read_events_unlocked())


def _control_state_unlocked(events: list[dict] | None = None) -> dict:
    current = {"emergency_stopped": False, "updated_at": None}
    for event in events if events is not None else _read_events_unlocked():
        if event["kind"] == "emergency_stop":
            current = {"emergency_stopped": True, "updated_at": event["ts"]}
        elif event["kind"] == "emergency_unlock":
            current = {"emergency_stopped": False, "updated_at": event["ts"]}
    return current


def control_state() -> dict:
    with _PROCESS_LOCK, _FileLock():
        return _control_state_unlocked()


def is_emergency_stopped() -> bool:
    return bool(control_state()["emergency_stopped"])


def set_emergency_stop(actor: str = "local-user", capability_id: str | None = None) -> dict:
    with _PROCESS_LOCK, _FileLock():
        events = _read_events_unlocked()
        if _control_state_unlocked(events)["emergency_stopped"]:
            return _control_state_unlocked(events)
        _append_event_unlocked("emergency_stop", {
            "actor": actor,
            "capability_id": capability_id,
            "reason": "operator_requested",
        }, actor=actor, role="operator", capability=capability_id)
        return _control_state_unlocked(_read_events_unlocked())


def clear_emergency_stop(actor: str = "local-user", capability_id: str | None = None) -> dict:
    with _PROCESS_LOCK, _FileLock():
        events = _read_events_unlocked()
        if not _control_state_unlocked(events)["emergency_stopped"]:
            return _control_state_unlocked(events)
        _append_event_unlocked("emergency_unlock", {
            "actor": actor,
            "capability_id": capability_id,
            "reason": "operator_requested",
        }, actor=actor, role="operator", capability=capability_id)
        return _control_state_unlocked(_read_events_unlocked())


def _get_unlocked(decision_id: str) -> dict | None:
    state = _all_states_unlocked().get(decision_id)
    return copy.deepcopy(state) if state else None


def _project_status(state: dict) -> dict:
    """Project time-sensitive lifecycle state without mutating the journal."""
    projected = copy.deepcopy(state)
    if projected.get("status") in {STATUS_AWAITING_APPROVAL, STATUS_APPROVED}:
        try:
            expires_at = datetime.fromisoformat(
                str(projected.get("expires_at", "")).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                projected["status"] = STATUS_EXPIRED
        except (TypeError, ValueError):
            projected["status"] = STATUS_UNKNOWN
    return projected


def _expired(state: dict) -> bool:
    try:
        expires_at = datetime.fromisoformat(
            str(state.get("expires_at", "")).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def create_proposal(proposal: dict) -> dict:
    ok, errors = validate_proposal(proposal)
    if not ok:
        raise ValueError("invalid proposal: " + "; ".join(errors))
    with _PROCESS_LOCK, _FileLock():
        if _control_state_unlocked()["emergency_stopped"]:
            raise AuthorizationError("emergency stop is active")
        states = _all_states_unlocked()
        for existing in states.values():
            if existing["proposal"].get("idempotency_key") == proposal["idempotency_key"]:
                if existing["proposal_hash"] != proposal["proposal_hash"]:
                    raise ConflictError("idempotency key is bound to another proposal hash")
                return copy.deepcopy(existing)
            if existing["decision_id"] == proposal["decision_id"]:
                if existing["proposal_hash"] != proposal["proposal_hash"]:
                    raise ConflictError("decision_id is bound to another proposal hash")
                return copy.deepcopy(existing)
        parent_id = proposal.get("parent_decision_id")
        if parent_id:
            parent = states.get(parent_id)
            if parent is None:
                raise StateError("parent decision does not exist")
            if parent["status"] not in {
                    STATUS_REJECTED, STATUS_REPLAN_REQUESTED, STATUS_NEEDS_CLARIFICATION}:
                raise StateError("parent decision is not eligible for replanning")
            if proposal["version"] != parent["version"] + 1:
                raise ConflictError("replan version must increment the parent version")
        stored = copy.deepcopy(proposal)
        stored["status"] = STATUS_AWAITING_APPROVAL
        _append_event_unlocked("proposal_created", stored, policy_version=stored["policy"]["version"])
        return _get_unlocked(stored["decision_id"])


def get_decision(decision_id: str) -> dict | None:
    with _PROCESS_LOCK, _FileLock():
        state = _get_unlocked(decision_id)
        return _project_status(state) if state else None


def list_pending_decisions() -> list[dict]:
    with _PROCESS_LOCK, _FileLock():
        states = _all_states_unlocked()
        pending = {
            STATUS_AWAITING_APPROVAL, STATUS_REPLAN_REQUESTED, STATUS_NEEDS_CLARIFICATION
        }
        result = []
        for state in states.values():
            projected = _project_status(state)
            if projected["status"] in pending:
                result.append(projected)
        return result


def _find_feedback(state: dict, feedback_id: str) -> dict | None:
    return next((item for item in state["feedback"] if item["feedback_id"] == feedback_id), None)


def record_feedback(decision_id: str, feedback: dict) -> dict:
    ok, errors = validate_feedback(feedback)
    if not ok:
        raise ValueError("invalid feedback: " + "; ".join(errors))
    if feedback["decision_id"] != decision_id:
        raise ValueError("feedback decision_id does not match decision")
    with _PROCESS_LOCK, _FileLock():
        state = _get_unlocked(decision_id)
        if state is None:
            raise KeyError(decision_id)
        duplicate = _find_feedback(state, feedback["feedback_id"])
        if duplicate:
            return state
        if state["status"] in {STATUS_REPLAN_REQUESTED, STATUS_NEEDS_CLARIFICATION}:
            return state
        if state["status"] != STATUS_AWAITING_APPROVAL:
            raise StateError(f"cannot record feedback while {state['status']}")
        if feedback["verdict"] == "reject":
            attempt = state["attempt"] + 1
            if attempt > MAX_REPLAN_ATTEMPTS:
                status = STATUS_NEEDS_CLARIFICATION
                request_id = None
            else:
                status = STATUS_REPLAN_REQUESTED
                request_id = str(uuid.uuid4())
            _append_event_unlocked(
                "rejection_transaction",
                {
                    "decision_id": decision_id,
                    "feedback": copy.deepcopy(feedback),
                    "attempt": attempt,
                    "replan_request_id": request_id,
                    "status": status,
                },
            )
        else:
            _append_event_unlocked("feedback_recorded", {
                "decision_id": decision_id,
                "feedback": copy.deepcopy(feedback),
            })
        return _get_unlocked(decision_id)


def request_replan(decision_id: str, feedback_id: str) -> dict:
    with _PROCESS_LOCK, _FileLock():
        state = _get_unlocked(decision_id)
        if state is None:
            raise KeyError(decision_id)
        feedback = _find_feedback(state, feedback_id)
        if feedback is None:
            raise KeyError(feedback_id)
        if state["replan_request_id"]:
            return {
                "decision_id": decision_id,
                "replan_request_id": state["replan_request_id"],
                "status": state["status"],
                "feedback": copy.deepcopy(feedback),
            }
        if feedback["verdict"] != "reject":
            raise StateError("only rejected feedback can request a replan")
        return {
            "decision_id": decision_id,
            "replan_request_id": None,
            "status": state["status"],
            "feedback": copy.deepcopy(feedback),
        }


def approve(decision_id: str, proposal_hash_value: str, reviewer_id: str, capability: str) -> dict:
    if not isinstance(capability, str) or not capability:
        raise AuthorizationError("operator capability is required")
    with _PROCESS_LOCK, _FileLock():
        if _control_state_unlocked()["emergency_stopped"]:
            raise AuthorizationError("emergency stop is active")
        state = _get_unlocked(decision_id)
        if state is None:
            raise KeyError(decision_id)
        if _expired(state):
            raise AuthorizationError("proposal_expired")
        if state["proposal_hash"] != proposal_hash_value:
            raise ConflictError("proposal hash does not match")
        if state["status"] != STATUS_AWAITING_APPROVAL:
            raise AuthorizationError(f"cannot approve while {state['status']}")
        capability_id = _digest(capability)
        events = _read_events_unlocked()
        if any(event.get("kind") == "approval_recorded"
               and event.get("payload", {}).get("capability_id") == capability_id
               for event in events):
            raise AuthorizationError("operator capability was already consumed")
        _append_event_unlocked("approval_recorded", {
            "decision_id": decision_id,
            "proposal_hash": proposal_hash_value,
            "reviewer_id": reviewer_id,
            "capability_id": capability_id,
        }, actor=reviewer_id, role="operator", capability=capability,
        policy_version=state["proposal"]["policy"]["version"])
        return _get_unlocked(decision_id)


def execute_approved(decision_id: str, proposal_hash_value: str, request_id: str) -> dict:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id is required")
    with _PROCESS_LOCK, _FileLock():
        if _control_state_unlocked()["emergency_stopped"]:
            raise AuthorizationError("emergency stop is active")
        state = _get_unlocked(decision_id)
        if state is None:
            raise KeyError(decision_id)
        if _expired(state):
            raise AuthorizationError("proposal_expired")
        if state["proposal_hash"] != proposal_hash_value:
            raise ConflictError("proposal hash does not match")
        if state["status"] != STATUS_APPROVED:
            raise AuthorizationError("execution requires an approved proposal")
        if state["execution"]:
            if state["execution"].get("request_id") == request_id:
                return copy.deepcopy(state["execution"])
            raise ConflictError("decision already has another execution request")
        accepted = {
            "decision_id": decision_id,
            "proposal_hash": proposal_hash_value,
            "request_id": request_id,
            "state": EXECUTION_ACCEPTED,
        }
        _append_event_unlocked("execution_accepted", accepted, execution_id=request_id)
        return accepted


def append_execution_result(decision_id: str, result: dict) -> dict:
    if not isinstance(result, dict):
        raise ValueError("execution result must be an object")
    state_name = result.get("state")
    if state_name not in {EXECUTION_ACCEPTED, EXECUTION_DISPATCHED, EXECUTION_SENT, EXECUTION_UNKNOWN}:
        raise ValueError("execution result state is invalid")
    with _PROCESS_LOCK, _FileLock():
        state = _get_unlocked(decision_id)
        if state is None:
            raise KeyError(decision_id)
        if state["status"] not in {STATUS_APPROVED, STATUS_UNKNOWN, STATUS_EXECUTED}:
            raise StateError(f"cannot record execution while {state['status']}")
        if state["execution"] and state["execution"].get("request_id") != result.get("request_id"):
            raise ConflictError("execution request does not match")
        status = STATUS_UNKNOWN if state_name == EXECUTION_UNKNOWN else STATUS_EXECUTED
        payload = {
            "decision_id": decision_id,
            "request_id": result.get("request_id"),
            "state": state_name,
            "status": status,
            "result": copy.deepcopy(result),
        }
        if state["execution"] and state["execution"].get("state") == state_name:
            return state
        _append_event_unlocked("execution_result", payload, execution_id=result.get("request_id"))
        return _get_unlocked(decision_id)
