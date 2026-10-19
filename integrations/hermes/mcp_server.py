"""Sole model-facing stdio MCP adapter for the ClaudeManager host gateway.

The adapter keeps REST details behind narrow MCP tools. It never exposes the host URL,
WebSocket, PTY, filesystem, approval, or operator capabilities as tools.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

# Hermes launches this file directly, so the repository root is not otherwise on
# sys.path. Keep the adapter executable from any working directory without asking
# Hermes for arbitrary filesystem access.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from terminal.agent_contracts import (
    EXECUTION_SENT, EXECUTION_UNKNOWN, MODEL_NAME, MODEL_PROVIDER, STATUS_APPROVED, proposal_hash,
)


class McpProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeAttestation:
    hermes_profile: str
    hermes_version: str
    provider: str
    model: str
    config_hash: str
    session_id: str

    def verify(self) -> None:
        if self.provider != MODEL_PROVIDER or self.model != MODEL_NAME:
            raise ValueError("effective Hermes runtime must be openai-codex/gpt-5.6-luna")
        if len(self.config_hash) != 64 or any(c not in "0123456789abcdefABCDEF" for c in self.config_hash):
            raise ValueError("runtime config_hash must be sha256")
        if not all(isinstance(value, str) and value.strip() for value in asdict(self).values()):
            raise ValueError("runtime attestation is incomplete")

    def as_proposal_attestation(self) -> dict:
        self.verify()
        return {
            "hermes_profile": self.hermes_profile,
            "hermes_version": self.hermes_version,
            "config_hash": self.config_hash,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
        }

    @classmethod
    def from_environment(cls) -> "RuntimeAttestation":
        values = {
            "hermes_profile": os.environ.get("HERMES_PROFILE", ""),
            "hermes_version": os.environ.get("HERMES_VERSION", ""),
            "provider": os.environ.get("HERMES_PROVIDER", ""),
            "model": os.environ.get("HERMES_MODEL", ""),
            "config_hash": os.environ.get("HERMES_CONFIG_HASH", ""),
            "session_id": os.environ.get("HERMES_SESSION_ID", ""),
        }
        result = cls(**values)
        result.verify()
        return result


class McpServer:
    def __init__(self, gateway_url: str, gateway_token: str, attestation: RuntimeAttestation,
                 request_fn=None):
        parsed_gateway = urlsplit(gateway_url)
        if parsed_gateway.scheme != "http" or parsed_gateway.hostname not in {
                "127.0.0.1", "host.docker.internal"} or not parsed_gateway.port:
            raise ValueError("MCP adapter gateway must use an authenticated known local host")
        if not gateway_token:
            raise ValueError("gateway token is required")
        attestation.verify()
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.attestation = attestation
        self._request_fn = request_fn or self._http_request

    def tools(self) -> list[dict]:
        decision_schema = {
            "type": "object",
            "properties": {"decision_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["decision_id"], "additionalProperties": False,
        }
        # open_session is automatic now: no decision_id/proposal_hash, because there
        # is no proposal and no approval for it anymore.
        open_schema = {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "group_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["request_id", "group_id"],
            "additionalProperties": False,
        }
        # link_work_item is automatic too, same shape as open_session: no
        # decision_id/proposal_hash, because there is no proposal for it either.
        link_schema = {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "session_key": {"type": "string", "minLength": 1, "maxLength": 128},
                "work_item_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["request_id", "session_key", "work_item_id"],
            "additionalProperties": False,
        }
        owned_prompt_schema = {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "session_key": {"type": "string", "minLength": 1, "maxLength": 128},
                "text": {"type": "string", "minLength": 1, "maxLength": 16000},
            },
            "required": ["request_id", "session_key", "text"],
            "additionalProperties": False,
        }
        return [
            {"name": "get_health", "description": "Read host gateway health", "inputSchema": {"type": "object"}},
            {"name": "send_owned_prompt", "description": "Send a prompt, with no approval, to a tab YOU opened with open_session in an allow-listed group (see get_health.auto_prompt_groups); idempotent per request_id. Tabs the user opened still need submit_proposal", "inputSchema": owned_prompt_schema},
            {"name": "list_sessions", "description": "List managed session snapshots", "inputSchema": {"type": "object"}},
            {"name": "list_work_items", "description": "Read work-item context", "inputSchema": {"type": "object"}},
            {"name": "get_work_overview", "description": "Read the daily work overview", "inputSchema": {"type": "object"}},
            {"name": "poll_sources", "description": "Read new checkpointed Jira/Teams context", "inputSchema": {"type": "object"}},
            {"name": "search_jira", "description": "Search Jira context read-only", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
            {"name": "search_teams", "description": "Search Teams context read-only", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
            {"name": "submit_proposal", "description": "Submit an immutable action proposal for local approval", "inputSchema": {"type": "object", "properties": {"proposal": {"type": "object"}}, "required": ["proposal"]}},
            {"name": "get_decision", "description": "Read the redacted state of one proposal after local review", "inputSchema": decision_schema},
            {"name": "open_session", "description": "Open one configured conversational session automatically — no proposal, no approval; idempotent per request_id and journaled by the host", "inputSchema": open_schema},
            {"name": "link_work_item", "description": "Link a managed session to a Work Item automatically — no proposal, no approval; idempotent per request_id and journaled by the host", "inputSchema": link_schema},
            {"name": "get_execution_receipt", "description": "Read the host's dispatch receipt for an approved send_prompt decision — the host dispatches on approval, this tool never triggers a write", "inputSchema": decision_schema},
            {"name": "get_replan", "description": "Retrieve actionable rejection feedback for replanning", "inputSchema": {"type": "object", "properties": {"decision_id": {"type": "string"}}, "required": ["decision_id"]}},
        ]

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise McpProtocolError("invalid JSON-RPC request")
        method = request.get("method")
        params = request.get("params") or {}
        # MCP lifecycle notifications intentionally do not receive a JSON-RPC
        # response. Returning an error response for them makes the stdio client
        # treat the adapter as protocol-invalid and eventually park the server.
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            if "id" not in request:
                return None
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        if "id" not in request:
            raise McpProtocolError("request id is required")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claudemanager-hermes-bridge", "version": "1.0"},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": self.tools()}}
        if method != "tools/call":
            raise McpProtocolError("unsupported MCP method")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            raise McpProtocolError("tools/call requires name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpProtocolError("tools/call arguments must be an object")
        data, is_error = self._call_tool(params["name"], arguments)
        return {"jsonrpc": "2.0", "id": request["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data,
            "isError": is_error,
        }}

    def _call_tool(self, name: str, args: dict) -> tuple[dict, bool]:
        paths = {
            "get_health": ("GET", "/v1/health"),
            "list_sessions": ("GET", "/v1/sessions"),
            "list_work_items": ("GET", "/v1/work-items"),
            "get_work_overview": ("GET", "/v1/work-overview"),
            "poll_sources": ("GET", "/v1/agent-sources"),
        }
        if name in paths:
            status, data = self._request_fn(*paths[name])
            return self._business_result(status, self._envelope(data, "claudemanager"))
        if name in {"search_jira", "search_teams"}:
            query = args.get("q")
            if not isinstance(query, str) or not query.strip():
                raise McpProtocolError("search requires q")
            source = "jira" if name == "search_jira" else "teams"
            path = f"/v1/{source}/search?q={urllib.parse.quote(query, safe='')}"
            status, data = self._request_fn("GET", path)
            return self._business_result(status, self._envelope(data, source))
        if name == "submit_proposal":
            proposal = args.get("proposal")
            if not isinstance(proposal, dict):
                raise McpProtocolError("submit_proposal requires proposal")
            prepared = self._attest_proposal(proposal)
            status, data = self._request_fn("POST", "/v1/proposals", prepared)
            return self._business_result(status, data)
        if name == "get_decision":
            decision_id = self._strict_identifier_args(args, {"decision_id"}, "get_decision")
            status, data = self._request_fn(
                "GET", "/v1/proposals/" + urllib.parse.quote(decision_id, safe=""))
            return self._business_result(status, data)
        if name == "open_session":
            self._validate_automatic_args(args, "open_session", {"group_id"})
            payload = {key: args[key] for key in ("request_id", "group_id")}
            status, data = self._request_fn("POST", "/v1/sessions", payload)
            return self._business_result(status, data)
        if name == "link_work_item":
            self._validate_automatic_args(args, "link_work_item", {"session_key", "work_item_id"})
            payload = {key: args[key] for key in ("request_id", "session_key", "work_item_id")}
            status, data = self._request_fn("POST", "/v1/work-items/link", payload)
            return self._business_result(status, data)
        if name == "send_owned_prompt":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise McpProtocolError("send_owned_prompt.text is invalid")
            ids = {key: value for key, value in args.items() if key != "text"}
            self._validate_automatic_args(ids, "send_owned_prompt", {"session_key"})
            path = "/v1/sessions/" + urllib.parse.quote(ids["session_key"], safe="") + "/prompt"
            status, data = self._request_fn(
                "POST", path, {"request_id": ids["request_id"], "text": text})
            return self._business_result(status, data)
        if name == "get_execution_receipt":
            decision_id = self._strict_identifier_args(args, {"decision_id"}, "get_execution_receipt")
            status, data = self._request_fn(
                "GET", "/v1/proposals/" + urllib.parse.quote(decision_id, safe=""))
            if status >= 400:
                return self._business_result(status, data)
            receipt = self._receipt_from_decision(data.get("decision") or {})
            return self._business_result(200, receipt)
        if name == "get_replan":
            decision_id = args.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id:
                raise McpProtocolError("get_replan requires decision_id")
            status, data = self._request_fn("GET", "/v1/replans/" + urllib.parse.quote(decision_id, safe=""))
            result, _ = self._business_result(status, self._envelope(data, "replan"))
            # Rejection feedback is deliberately a business error so Hermes starts
            # another reasoning cycle instead of treating the feedback as completion.
            return result, True
        raise McpProtocolError("unknown MCP tool")

    def _attest_proposal(self, source: dict) -> dict:
        proposal = copy.deepcopy(source)
        model = proposal.get("model")
        if not isinstance(model, dict) or model.get("provider") != MODEL_PROVIDER or model.get("name") != MODEL_NAME:
            raise McpProtocolError("proposal model must be effective Luna")
        model["attestation"] = self.attestation.as_proposal_attestation()
        proposal["proposal_hash"] = proposal_hash(proposal)
        return proposal

    @staticmethod
    def _business_result(status: int, data: dict) -> tuple[dict, bool]:
        safe = McpServer._sanitize(data)
        return safe, status >= 400 or McpServer._contains_unknown(safe)

    @staticmethod
    def _sanitize(value):
        blocked = {"token", "access_token", "authorization", "operator_capability",
                   "gateway_url", "websocket", "pty_id", "path", "filesystem"}
        if isinstance(value, dict):
            return {key: McpServer._sanitize(item) for key, item in value.items()
                    if str(key).lower() not in blocked}
        if isinstance(value, list):
            return [McpServer._sanitize(item) for item in value]
        return value

    @staticmethod
    def _contains_unknown(value) -> bool:
        if isinstance(value, dict):
            # "UNKNOWN" is the raw journal execution state (still visible via
            # get_decision); PROVIDER_PROCESSING_UNKNOWN is the same honesty
            # expressed in get_execution_receipt's own vocabulary.
            if value.get("state") in {"UNKNOWN", "PROVIDER_PROCESSING_UNKNOWN"}:
                return True
            return any(McpServer._contains_unknown(item) for item in value.values())
        if isinstance(value, list):
            return any(McpServer._contains_unknown(item) for item in value)
        return False

    @staticmethod
    def _receipt_from_decision(decision: dict) -> dict:
        """Translate a redacted decision (GET /v1/proposals/<id>) into the same
        receipt vocabulary the host uses at dispatch time (see api/bridge.py).

        This is a read, not a write: it never triggers a PTY write and never
        claims PROVIDER_COMPLETED. WRITE_FAILED is deliberately absent here -- a
        pre-write failure leaves nothing in the journal, so from this read the
        decision looks identical to "approved, not yet dispatched" (DISPATCHED).
        That is fine: only the host UI retries, Hermes never should.
        """
        execution = decision.get("execution") or {}
        exec_state = execution.get("state")
        if exec_state == EXECUTION_SENT:
            state = "HOST_WRITE_ACCEPTED"
        elif exec_state == EXECUTION_UNKNOWN:
            state = "PROVIDER_PROCESSING_UNKNOWN"
        elif decision.get("status") == STATUS_APPROVED:
            state = "DISPATCHED"
        else:
            state = "APPROVAL_REQUIRED"
        return {
            "decision_id": decision.get("decision_id"),
            "request_id": execution.get("request_id"),
            "state": state,
            "retryable": False,
        }

    @staticmethod
    def _strict_identifier_args(args: dict, expected: set[str], name: str) -> str:
        if set(args) != expected:
            raise McpProtocolError(f"{name} requires exactly: {', '.join(sorted(expected))}")
        value = args[next(iter(expected))]
        if (not isinstance(value, str) or not value.strip() or len(value) > 128
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)):
            raise McpProtocolError(f"{name} identifier is invalid")
        return value

    @staticmethod
    def _validate_automatic_args(args: dict, name: str, extra: set[str]) -> None:
        """Strict args for automatic-action tools (currently only open_session).

        No proposal_hash and no free-text field exist here — every field is a
        safe identifier, so this is intentionally a tighter validator than the
        loose free-text pattern a proposal payload allows.
        """
        expected = {"request_id"} | extra
        if set(args) != expected:
            raise McpProtocolError(f"{name} requires exactly: {', '.join(sorted(expected))}")
        for field in expected:
            value = args[field]
            if (not isinstance(value, str) or not value.strip() or len(value) > 128
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)):
                raise McpProtocolError(f"{name}.{field} is invalid")

    @staticmethod
    def _envelope(data: dict, source: str) -> dict:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "source": source,
            "trust": "external_data",
            "content_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "data": data,
        }

    def _http_request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.gateway_url + path, data=body, method=method,
            headers={"Authorization": f"Bearer {self.gateway_token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except (OSError, json.JSONDecodeError):
                return exc.code, {"error": "gateway_http_error"}
        except (OSError, json.JSONDecodeError):
            return 503, {"error": "gateway_unavailable"}


def main() -> int:
    try:
        gateway_token = os.environ.get("CLAUDEMANAGER_HERMES_TOKEN")
        if not gateway_token:
            token_file = os.environ["CLAUDEMANAGER_HERMES_TOKEN_FILE"]
            with open(token_file, encoding="utf-8") as handle:
                gateway_token = handle.read().strip()
        server = McpServer(
            os.environ.get("CLAUDEMANAGER_AGENT_GATEWAY_URL", "http://127.0.0.1:8787"),
            gateway_token,
            RuntimeAttestation.from_environment(),
        )
    except (KeyError, ValueError) as exc:
        print(f"Hermes MCP adapter disabled: {exc}", file=sys.stderr)
        return 2
    for line in sys.stdin:
        try:
            response = server.handle(json.loads(line))
        except (json.JSONDecodeError, McpProtocolError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
