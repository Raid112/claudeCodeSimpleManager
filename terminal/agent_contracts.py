"""Pure, deterministic contracts for the Hermes <-> ClaudeManager boundary.

This module deliberately has no access to PTYs, files, network clients, or UI state.
It is the first policy gate: only the small semantic action allowlist and the verified
Luna model can reach the approval state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any


MODEL_PROVIDER = "openai-codex"
MODEL_NAME = "gpt-5.6-luna"

ACTION_OPEN_SESSION = "open_session"
ACTION_SEND_PROMPT = "send_prompt"
ACTION_LINK_WORK_ITEM = "link_work_item"
ALLOWED_ACTIONS = frozenset({
    ACTION_OPEN_SESSION,
    ACTION_SEND_PROMPT,
    ACTION_LINK_WORK_ITEM,
})

STATUS_PROPOSED = "proposed"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REPLAN_REQUESTED = "replan_requested"
STATUS_ANALYZING_AGAIN = "analyzing_again"
STATUS_EXECUTED = "executed"
STATUS_EXPIRED = "expired"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_UNKNOWN = "unknown"

EXECUTION_ACCEPTED = "ACCEPTED"
EXECUTION_DISPATCHED = "DISPATCHED"
EXECUTION_SENT = "SENT"
EXECUTION_UNKNOWN = "UNKNOWN"

VERDICTS = frozenset({"approve", "reject", "modify", "clarify", "expire"})
REASON_CODES = frozenset({
    "wrong_target",
    "missing_context",
    "too_risky",
    "wrong_scope",
    "timing",
    "other",
})
FEEDBACK_SCOPES = frozenset({"this_proposal", "same_work_item", "candidate_heuristic"})
TRUST_VALUES = frozenset({"external_data"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
POLICY_DECISIONS = frozenset({"approval_required", "deny", "allow"})

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROPOSAL_FIELDS = frozenset({
    "decision_id", "parent_decision_id", "trace_id", "version", "context_refs",
    "intent", "action", "risk", "policy", "model", "proposal_hash", "status",
    "idempotency_key", "created_at", "expires_at",
})
_CONTEXT_FIELDS = frozenset({"source", "object_id", "retrieved_at", "content_hash", "trust"})
_ACTION_FIELDS = frozenset({"type", "target", "parameters", "expected_outcome", "reversible"})
_RISK_FIELDS = frozenset({"level", "factors"})
_POLICY_FIELDS = frozenset({"version", "decision", "constraints"})
_MODEL_FIELDS = frozenset({"provider", "name", "prompt_version", "attestation"})
_ATTESTATION_FIELDS = frozenset({
    "hermes_profile", "hermes_version", "config_hash", "session_id", "provider", "model"
})
_FEEDBACK_FIELDS = frozenset({
    "feedback_id", "decision_id", "reviewer_id", "verdict", "reason_code", "comment",
    "scope", "requested_changes", "created_at",
})


def _unknown(data: dict, allowed: frozenset[str], prefix: str) -> list[str]:
    return [f"{prefix}.{key} is not allowed" for key in sorted(set(data) - allowed)]


def _uuid(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{field} must be a UUID")
        return
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        errors.append(f"{field} must be a UUID")


def _safe_text(value: Any, field: str, errors: list[str], max_len: int = 4000) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
    elif len(value) > max_len:
        errors.append(f"{field} exceeds {max_len} characters")


def _identifier(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        errors.append(f"{field} must be a safe identifier")


def _hash(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        errors.append(f"{field} must be a sha256 hex digest")


def _timestamp(value: Any, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{field} must be ISO-8601")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            errors.append(f"{field} must include a timezone")
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        errors.append(f"{field} must be ISO-8601")
        return None


def _canonical_payload(payload: dict) -> bytes:
    data = copy.deepcopy(payload)
    data.pop("proposal_hash", None)
    data.pop("status", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def proposal_hash(payload: dict) -> str:
    """Return the hash of the immutable proposal payload.

    ``proposal_hash`` and mutable lifecycle ``status`` are excluded so a decision can
    transition without changing the identity of the approved payload.
    """
    if not isinstance(payload, dict):
        raise ValueError("proposal must be a dictionary")
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def validate_model_attestation(attestation: dict) -> None:
    if not isinstance(attestation, dict):
        raise ValueError("model attestation must be an object")
    unknown = _unknown(attestation, _ATTESTATION_FIELDS, "attestation")
    if unknown:
        raise ValueError("; ".join(unknown))
    for field in ("hermes_profile", "hermes_version", "session_id"):
        if not isinstance(attestation.get(field), str) or not attestation[field].strip():
            raise ValueError(f"attestation.{field} is required")
    if not _HEX64.fullmatch(str(attestation.get("config_hash", ""))):
        raise ValueError("attestation.config_hash must be a sha256 hex digest")
    if "provider" in attestation and attestation["provider"] != MODEL_PROVIDER:
        raise ValueError("attestation.provider must be openai-codex")
    if "model" in attestation and attestation["model"] != MODEL_NAME:
        raise ValueError("attestation.model must be gpt-5.6-luna")


def _validate_action(action: Any, errors: list[str]) -> None:
    if not isinstance(action, dict):
        errors.append("action must be an object")
        return
    errors.extend(_unknown(action, _ACTION_FIELDS, "action"))
    action_type = action.get("type")
    if action_type not in ALLOWED_ACTIONS:
        errors.append("action.type is unknown or deny-always")
        return
    if not isinstance(action.get("target"), dict):
        errors.append("action.target must be an object")
    if not isinstance(action.get("parameters"), dict):
        errors.append("action.parameters must be an object")
    _safe_text(action.get("expected_outcome"), "action.expected_outcome", errors, 1000)
    if action.get("reversible") is not True:
        errors.append("action.reversible must be true")
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
    if action_type == ACTION_OPEN_SESSION:
        _identifier(target.get("group_id"), "action.target.group_id", errors)
        if target.keys() - {"group_id"}:
            errors.append("action.target has unsupported open_session fields")
    elif action_type == ACTION_SEND_PROMPT:
        _identifier(target.get("session_key"), "action.target.session_key", errors)
        _safe_text(parameters.get("text"), "action.parameters.text", errors, 12000)
        if target.keys() - {"session_key"}:
            errors.append("action.target has unsupported send_prompt fields")
        if parameters.keys() - {"text"}:
            errors.append("action.parameters has unsupported send_prompt fields")
    elif action_type == ACTION_LINK_WORK_ITEM:
        _identifier(target.get("session_key"), "action.target.session_key", errors)
        _identifier(parameters.get("work_item_id"), "action.parameters.work_item_id", errors)
        if target.keys() - {"session_key"}:
            errors.append("action.target has unsupported link_work_item fields")
        if parameters.keys() - {"work_item_id"}:
            errors.append("action.parameters has unsupported link_work_item fields")


def validate_proposal(payload: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["proposal must be an object"]
    errors.extend(_unknown(payload, _PROPOSAL_FIELDS, "proposal"))
    required = (
        "decision_id", "trace_id", "version", "context_refs", "intent", "action", "risk",
        "policy", "model", "proposal_hash", "idempotency_key", "created_at", "expires_at",
    )
    for field in required:
        if field not in payload:
            errors.append(f"proposal.{field} is required")
    if errors:
        return False, errors

    _uuid(payload["decision_id"], "proposal.decision_id", errors)
    if payload.get("parent_decision_id") is not None:
        _uuid(payload["parent_decision_id"], "proposal.parent_decision_id", errors)
    _uuid(payload["trace_id"], "proposal.trace_id", errors)
    if not isinstance(payload["version"], int) or payload["version"] < 1:
        errors.append("proposal.version must be a positive integer")
    _safe_text(payload["intent"], "proposal.intent", errors, 4000)
    _hash(payload["proposal_hash"], "proposal.proposal_hash", errors)
    try:
        validate_idempotency(payload["idempotency_key"], payload["proposal_hash"])
    except ValueError as exc:
        errors.append(str(exc))

    created = _timestamp(payload["created_at"], "proposal.created_at", errors)
    expires = _timestamp(payload["expires_at"], "proposal.expires_at", errors)
    if created and expires and expires <= created:
        errors.append("proposal.expires_at must be after created_at")
    if expires and expires <= datetime.now(timezone.utc):
        errors.append("proposal is expired")

    refs = payload["context_refs"]
    if not isinstance(refs, list) or not refs:
        errors.append("proposal.context_refs must be a non-empty list")
    else:
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                errors.append(f"proposal.context_refs[{index}] must be an object")
                continue
            errors.extend(_unknown(ref, _CONTEXT_FIELDS, f"context_refs[{index}]"))
            for field in _CONTEXT_FIELDS:
                if field not in ref:
                    errors.append(f"context_refs[{index}].{field} is required")
            if ref.get("source") not in {"jira", "teams", "todo", "session"}:
                errors.append(f"context_refs[{index}].source is invalid")
            _safe_text(ref.get("object_id"), f"context_refs[{index}].object_id", errors, 256)
            _timestamp(ref.get("retrieved_at"), f"context_refs[{index}].retrieved_at", errors)
            _hash(ref.get("content_hash"), f"context_refs[{index}].content_hash", errors)
            if ref.get("trust") not in TRUST_VALUES:
                errors.append(f"context_refs[{index}].trust must be external_data")

    action = payload["action"]
    _validate_action(action, errors)
    risk = payload["risk"]
    if not isinstance(risk, dict):
        errors.append("risk must be an object")
    else:
        errors.extend(_unknown(risk, _RISK_FIELDS, "risk"))
        if risk.get("level") not in RISK_LEVELS:
            errors.append("risk.level is invalid")
        if not isinstance(risk.get("factors"), list) or any(
                not isinstance(item, str) for item in risk.get("factors", [])):
            errors.append("risk.factors must be a list of strings")

    policy = payload["policy"]
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
    else:
        errors.extend(_unknown(policy, _POLICY_FIELDS, "policy"))
        _safe_text(policy.get("version"), "policy.version", errors, 64)
        if policy.get("decision") not in POLICY_DECISIONS:
            errors.append("policy.decision is invalid")
        if not isinstance(policy.get("constraints"), list) or any(
                not isinstance(item, str) for item in policy.get("constraints", [])):
            errors.append("policy.constraints must be a list of strings")
        if policy.get("decision") == "deny":
            errors.append("policy denies this proposal")

    model = payload["model"]
    if not isinstance(model, dict):
        errors.append("model must be an object")
    else:
        errors.extend(_unknown(model, _MODEL_FIELDS, "model"))
        if model.get("provider") != MODEL_PROVIDER or model.get("name") != MODEL_NAME:
            errors.append("model must be openai-codex/gpt-5.6-luna")
        for field in ("prompt_version", "attestation"):
            if field not in model:
                errors.append(f"model.{field} is required")
        try:
            validate_model_attestation(model.get("attestation"))
        except ValueError as exc:
            errors.append(str(exc))

    if "status" in payload:
        allowed_statuses = {
            STATUS_PROPOSED, STATUS_AWAITING_APPROVAL, STATUS_APPROVED, STATUS_REJECTED,
            STATUS_REPLAN_REQUESTED, STATUS_ANALYZING_AGAIN, STATUS_EXECUTED,
            STATUS_EXPIRED, STATUS_NEEDS_CLARIFICATION, STATUS_UNKNOWN,
        }
        if payload["status"] not in allowed_statuses:
            errors.append("proposal.status is invalid")

    try:
        if payload["proposal_hash"] != proposal_hash(payload):
            errors.append("proposal_hash does not match immutable payload")
    except (TypeError, ValueError, OverflowError) as exc:
        errors.append(f"proposal hash cannot be computed: {exc}")
    return not errors, errors


def validate_feedback(payload: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["feedback must be an object"]
    errors.extend(_unknown(payload, _FEEDBACK_FIELDS, "feedback"))
    for field in _FEEDBACK_FIELDS:
        if field not in payload:
            errors.append(f"feedback.{field} is required")
    if errors:
        return False, errors
    _uuid(payload["feedback_id"], "feedback.feedback_id", errors)
    _uuid(payload["decision_id"], "feedback.decision_id", errors)
    _safe_text(payload["reviewer_id"], "feedback.reviewer_id", errors, 128)
    if payload["verdict"] not in VERDICTS:
        errors.append("feedback.verdict is invalid")
    if payload["reason_code"] not in REASON_CODES:
        errors.append("feedback.reason_code is invalid")
    _safe_text(payload["comment"], "feedback.comment", errors, 4000)
    if payload["scope"] not in FEEDBACK_SCOPES:
        errors.append("feedback.scope is invalid")
    if not isinstance(payload["requested_changes"], list) or any(
            not isinstance(item, str) or not item.strip() for item in payload["requested_changes"]):
        errors.append("feedback.requested_changes must be a list of non-empty strings")
    _timestamp(payload["created_at"], "feedback.created_at", errors)
    return not errors, errors


def validate_idempotency(key: str, payload_hash_value: str) -> None:
    if not isinstance(key, str):
        raise ValueError("idempotency_key must be a UUID")
    try:
        uuid.UUID(key)
    except (ValueError, AttributeError):
        raise ValueError("idempotency_key must be a UUID") from None
    if not isinstance(payload_hash_value, str) or not _HEX64.fullmatch(payload_hash_value):
        raise ValueError("payload_hash must be a sha256 hex digest")


def can_transition(current: str, target: str) -> bool:
    transitions = {
        STATUS_PROPOSED: {STATUS_AWAITING_APPROVAL, STATUS_EXPIRED},
        STATUS_AWAITING_APPROVAL: {STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED},
        STATUS_REJECTED: {STATUS_REPLAN_REQUESTED, STATUS_NEEDS_CLARIFICATION},
        STATUS_REPLAN_REQUESTED: {STATUS_ANALYZING_AGAIN, STATUS_NEEDS_CLARIFICATION},
        STATUS_ANALYZING_AGAIN: {STATUS_PROPOSED, STATUS_NEEDS_CLARIFICATION},
        STATUS_APPROVED: {STATUS_EXECUTED, STATUS_UNKNOWN},
    }
    return target in transitions.get(current, set())
