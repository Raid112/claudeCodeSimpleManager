"""Authenticated, read-only local gateway for the Hermes integration.

The gateway is intentionally a small host boundary.  It receives live dependencies by
injection, exposes only versioned context reads in this checkpoint, and never forwards a
browser WebSocket, PTY handle, filesystem path, or credential to Hermes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from terminal.agent_contracts import ACTION_OPEN_SESSION, ACTION_SEND_PROMPT, STATUS_APPROVED, STATUS_EXPIRED
from terminal.agent_decisions import AuthorizationError, ConflictError
from terminal import agent_decisions
from terminal.input_protocol import InputProtocolError
from terminal.agent_sources import SourcePoller


class GatewayAuthError(RuntimeError):
    pass


class AgentGateway:
    def __init__(self, pty_manager, work_item_store, jira_adapter, teams_adapter, *,
                 host: str = "127.0.0.1", port: int = 8787,
                 hermes_token: str | None = None, operator_token: str | None = None,
                 groups: list[dict] | None = None,
                 max_body_bytes: int = 64 * 1024, max_query_chars: int = 256,
                 max_response_bytes: int = 512 * 1024, max_connections: int = 16):
        if host != "127.0.0.1":
            raise ValueError("the host POC must bind to 127.0.0.1")
        if not 0 <= int(port) <= 65535:
            raise ValueError("invalid gateway port")
        if not hermes_token:
            hermes_token = secrets.token_urlsafe(32)
        if not operator_token:
            operator_token = secrets.token_urlsafe(32)
        for name, token in (("hermes_token", hermes_token), ("operator_token", operator_token)):
            if not isinstance(token, str) or len(token) < 16:
                raise ValueError(f"{name} must be a high-entropy token")
        self.pty_manager = pty_manager
        self.work_item_store = work_item_store
        self.jira_adapter = jira_adapter
        self.teams_adapter = teams_adapter
        self.configured_groups = {
            str(item.get("group_id") or item.get("name")): {
                "group_id": str(item.get("group_id") or item.get("name")),
                "name": item.get("name"), "path": item.get("path"),
            }
            for item in (groups or [])
            if isinstance(item, dict) and (item.get("group_id") or item.get("name"))
        }
        self.host = host
        self.port = int(port)
        self.max_body_bytes = max(1024, int(max_body_bytes))
        self.max_query_chars = max(32, int(max_query_chars))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._max_connections = max(1, int(max_connections))
        self._connection_slots = threading.BoundedSemaphore(self._max_connections)
        self._token_lock = threading.RLock()
        self._hermes_token = hermes_token
        self._operator_token = operator_token
        self._revoked_hermes_tokens: set[str] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._emergency_stopped = False
        self._execution_locks: dict[str, threading.RLock] = {}
        self._execution_locks_guard = threading.Lock()
        self.source_poller = SourcePoller()

    @property
    def actual_port(self) -> int:
        return self._server.server_address[1] if self._server else self.port

    @property
    def emergency_stopped(self) -> bool:
        try:
            return self._emergency_stopped or agent_decisions.is_emergency_stopped()
        except Exception:
            return True

    def rotate_hermes_token(self, token: str | None = None) -> str:
        token = token or secrets.token_urlsafe(32)
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("Hermes token must be a high-entropy token")
        with self._token_lock:
            self._revoked_hermes_tokens.add(self._hermes_token)
            self._hermes_token = token
        return token

    def set_emergency_stop(self, capability: str) -> bool:
        with self._token_lock:
            if not secrets.compare_digest(str(capability), self._operator_token):
                return False
            self._emergency_stopped = True
            try:
                agent_decisions.set_emergency_stop("local-operator", "operator-capability")
            except Exception:
                return False
            return True

    def clear_emergency_stop(self, capability: str) -> bool:
        with self._token_lock:
            if not secrets.compare_digest(str(capability), self._operator_token):
                return False
            self._emergency_stopped = False
            try:
                agent_decisions.clear_emergency_stop("local-operator", "operator-capability")
            except Exception:
                return False
            return True

    def _is_hermes_token(self, token: str) -> bool:
        with self._token_lock:
            if token in self._revoked_hermes_tokens:
                return False
            return secrets.compare_digest(token, self._hermes_token)

    def _is_operator_token(self, token: str) -> bool:
        with self._token_lock:
            return secrets.compare_digest(token, self._operator_token)

    def start(self) -> None:
        if self._server is not None:
            return
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                # Never log request headers or query strings: they may contain external text.
                return

            def do_GET(self):
                gateway._dispatch(self, "GET")

            def do_POST(self):
                gateway._dispatch(self, "POST")

            def do_PUT(self):
                gateway._dispatch(self, "PUT")

            def do_DELETE(self):
                gateway._dispatch(self, "DELETE")

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name="claudemanager-agent-gateway", daemon=True)
            self._thread.start()
        except OSError:
            self._server = None
            self._thread = None
            raise

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self._send(handler, 429, {"error": "connection_limit"})
            return
        try:
            path = urlsplit(handler.path)
            query = parse_qs(path.query, keep_blank_values=True)
            if any(key.lower() in {"token", "access_token", "authorization"} for key in query):
                self._send(handler, 400, {"error": "credentials_must_use_authorization_header"})
                return
            token = self._bearer(handler)
            sensitive = path.path in {
                "/v1/proposals/approve", "/v1/proposals/reject", "/v1/execute",
                "/v1/emergency-stop", "/v1/unlock",
            }
            is_hermes = token is not None and self._is_hermes_token(token)
            is_operator = token is not None and self._is_operator_token(token)
            if not (is_hermes or is_operator):
                self._send(handler, 401, {"error": "unauthorized"})
                return
            if sensitive and is_hermes:
                self._send(handler, 403, {"error": "hermes_token_cannot_use_operator_route"})
                return
            if sensitive and not is_operator:
                self._send(handler, 403, {"error": "operator_capability_required"})
                return
            prompt_session_key = self._prompt_session_key(path.path)
            if prompt_session_key is not None and method == "POST":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                if self.emergency_stopped:
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                self._handle_prompt(handler, prompt_session_key)
                return
            if path.path == "/v1/sessions" and method == "POST":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                if self.emergency_stopped:
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                self._handle_session_create(handler)
                return
            if path.path == "/v1/proposals" and method == "POST":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                if self.emergency_stopped:
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                self._handle_proposal(handler)
                return
            if path.path == "/v1/proposals/pending" and method == "GET":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                self._send(handler, 200, {"proposals": [
                    self._redact_decision(item)
                    for item in agent_decisions.list_pending_decisions()
                ]})
                return
            if path.path.startswith("/v1/replans/") and method == "GET":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                decision_id = path.path.split("/", 3)[-1]
                self._handle_replan(handler, decision_id)
                return
            if method == "DELETE" and path.path.startswith("/v1/sessions/"):
                self._send(handler, 403, {"error": "session_delete_disabled_by_policy"})
                return
            if method != "GET":
                self._send(handler, 405, {"error": "method_not_allowed"})
                return
            if not is_hermes:
                self._send(handler, 403, {"error": "hermes_read_token_required"})
                return
            if len(handler.path) > self.max_query_chars + len(path.path) + 1:
                self._send(handler, 413, {"error": "query_limit"})
                return
            result = self._read_route(path.path, query)
            if result is None:
                self._send(handler, 404, {"error": "not_found"})
            else:
                status, body = result
                self._send(handler, status, body)
        finally:
            self._connection_slots.release()

    @staticmethod
    def _prompt_session_key(path: str) -> str | None:
        prefix = "/v1/sessions/"
        suffix = "/prompt"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        key = path[len(prefix):-len(suffix)]
        return key or None

    def _execution_lock(self, decision_id: str) -> threading.RLock:
        with self._execution_locks_guard:
            return self._execution_locks.setdefault(decision_id, threading.RLock())

    @staticmethod
    def _validate_prompt_decision(decision_id: str, proposal_hash: str,
                                  session_key: str, text: str) -> dict:
        decision = agent_decisions.get_decision(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        if decision["proposal_hash"] != proposal_hash:
            raise ConflictError("proposal hash does not match")
        action = decision["proposal"].get("action") or {}
        target = action.get("target") or {}
        parameters = action.get("parameters") or {}
        if action.get("type") != ACTION_SEND_PROMPT:
            raise ConflictError("proposal action type does not match send_prompt")
        if target.get("session_key") != session_key:
            raise ConflictError("proposal target does not match session_key")
        if parameters.get("text") != text:
            raise ConflictError("prompt does not match approved proposal")
        if decision["status"] == STATUS_EXPIRED:
            raise ConflictError("proposal_expired")
        if decision["status"] != STATUS_APPROVED:
            raise PermissionError("approved execution required")
        return decision

    def _handle_prompt(self, handler: BaseHTTPRequestHandler, session_key: str) -> None:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(handler, 400, {"error": "invalid_content_length"})
            return
        if length <= 0 or length > self.max_body_bytes:
            self._send(handler, 413, {"error": "body_limit"})
            return
        try:
            raw = handler.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(handler, 400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._send(handler, 400, {"error": "body_must_be_object"})
            return
        required = {"decision_id", "proposal_hash", "request_id", "text"}
        if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
            self._send(handler, 400, {"error": "prompt_contract"})
            return
        if not payload["text"] or not payload["request_id"]:
            self._send(handler, 400, {"error": "prompt_empty"})
            return
        try:
            self._validate_prompt_decision(
                payload["decision_id"], payload["proposal_hash"], session_key, payload["text"])
            receipt = self.pty_manager.send_prompt(
                session_key, payload["text"], payload["request_id"],
                payload["decision_id"], payload["proposal_hash"])
            self._send(handler, 200, {"receipt": receipt})
        except KeyError:
            self._send(handler, 404, {"error": "session_or_decision_not_found"})
        except PermissionError:
            self._send(handler, 403, {"error": "approved_execution_required"})
        except AuthorizationError as exc:
            self._send(handler, 403, {"error": str(exc)})
        except InputProtocolError as exc:
            self._send(handler, 400, {"error": str(exc)})
        except ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except ValueError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "prompt_execution_unavailable"})

    @staticmethod
    def _safe_configured_path(raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("configured group path is missing")
        if any(marker in raw_path for marker in ("%", "$", "..")):
            raise ValueError("configured group path contains expansion or traversal")
        if raw_path.startswith(("\\\\", "//", "\\\\.\\", "\\\\?\\")):
            raise ValueError("UNC/device paths are denied")
        if ":" in raw_path[2:]:
            raise ValueError("alternate data streams are denied")
        candidate = Path(raw_path)
        if candidate.is_symlink():
            raise ValueError("reparse/symlink group paths are denied")
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("configured group path is not a directory")
            # Windows reparse-point attribute; harmlessly absent on other platforms.
            if getattr(resolved.stat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse-point group paths are denied")
            return resolved
        except OSError as exc:
            raise ValueError("configured group path cannot be verified") from exc

    def _handle_session_create(self, handler: BaseHTTPRequestHandler) -> None:
        payload = self._read_json_body(handler)
        if payload is None:
            return
        required = {"decision_id", "proposal_hash", "request_id", "group_id"}
        if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
            self._send(handler, 400, {"error": "session_creation_contract"})
            return
        group_id = payload["group_id"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", group_id)
                or not payload["request_id"]):
            self._send(handler, 400, {"error": "invalid_group_id"})
            return
        group = self.configured_groups.get(group_id)
        if not group:
            self._send(handler, 403, {"error": "group_id_not_configured"})
            return
        try:
            with self._execution_lock(payload["decision_id"]):
                root = self._safe_configured_path(group["path"])
                decision = agent_decisions.get_decision(payload["decision_id"])
                if decision is None:
                    raise PermissionError("approved session proposal required")
                if decision["proposal_hash"] != payload["proposal_hash"]:
                    raise ConflictError("proposal hash does not match")
                if decision["status"] == STATUS_EXPIRED:
                    raise ConflictError("proposal_expired")
                action = decision["proposal"].get("action") or {}
                if action.get("type") != ACTION_OPEN_SESSION:
                    raise ConflictError("proposal action type does not match open_session")
                if (action.get("target") or {}).get("group_id") != group_id:
                    raise ConflictError("proposal target does not match group_id")
                existing = decision.get("execution") or {}
                if existing.get("request_id") == payload["request_id"]:
                    if existing.get("state") == "DISPATCHED":
                        self._send(handler, 201, {"session": existing.get("result") or existing})
                        return
                    if existing.get("state") == "ACCEPTED":
                        agent_decisions.append_execution_result(payload["decision_id"], {
                            "request_id": payload["request_id"], "state": "UNKNOWN",
                            "reason": "session creation outcome is unknown after accepted dispatch",
                        })
                        self._send(handler, 503, {"error": "session_creation_unknown"})
                        return
                if decision["status"] != STATUS_APPROVED:
                    raise PermissionError("approved session proposal required")
                agent_decisions.execute_approved(payload["decision_id"], payload["proposal_hash"], payload["request_id"])
                session = self.pty_manager.create_session(
                    group["name"], str(root), terminal_type="claude")
                public = {
                    "session_key": session.session_key,
                    "group_id": group_id,
                    "group_name": group["name"],
                    "terminal_type": "claude",
                    "state": "DISPATCHED",
                }
                agent_decisions.append_execution_result(payload["decision_id"], {
                    "request_id": payload["request_id"], "state": "DISPATCHED", "result": public,
                })
                self._send(handler, 201, {"session": public})
        except KeyError:
            self._send(handler, 404, {"error": "decision_not_found"})
        except PermissionError as exc:
            self._send(handler, 403, {"error": str(exc)})
        except AuthorizationError as exc:
            self._send(handler, 403, {"error": str(exc)})
        except ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except ValueError as exc:
            self._send(handler, 400, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "session_creation_unavailable"})

    @staticmethod
    def _bearer(handler: BaseHTTPRequestHandler) -> str | None:
        value = handler.headers.get("Authorization")
        if not value or not value.startswith("Bearer "):
            return None
        token = value[7:].strip()
        return token or None

    def _read_route(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict] | None:
        if path == "/v1/health":
            return 200, {
                "ok": True,
                "version": "v1",
                "status": "stopped" if self.emergency_stopped else "running",
                "model_requirement": "gpt-5.6-luna",
                "read_only": True,
            }
        if path == "/v1/sessions":
            return 200, {"sessions": self._sessions()}
        prefix = "/v1/proposals/"
        if path.startswith(prefix):
            decision_id = path[len(prefix):]
            if not decision_id or "/" in decision_id:
                return 404, {"error": "decision_not_found"}
            decision = agent_decisions.get_decision(decision_id)
            if decision is None:
                return 404, {"error": "decision_not_found"}
            return 200, {"decision": self._redact_decision(decision)}
        if path == "/v1/work-items":
            return 200, {"items": self._work_items()}
        if path == "/v1/work-overview":
            try:
                return 200, self.work_item_store.work_overview(2)
            except Exception:
                return 503, {"error": "work_overview_unavailable"}
        if path == "/v1/agent-sources":
            try:
                return 200, self.source_poller.poll(self.jira_adapter, self.teams_adapter)
            except Exception:
                return 503, {"error": "source_poll_unavailable"}
        if path in {"/v1/jira/search", "/v1/teams/search"}:
            value = (query.get("q") or [""])[0]
            if not value or len(value) > self.max_query_chars:
                return 413 if len(value) > self.max_query_chars else 400, {"error": "invalid_query"}
            try:
                if path == "/v1/jira/search":
                    return 200, {"results": self.jira_adapter.search_issues(value, 25)}
                return 200, {"results": self.teams_adapter.search_messages(value, 25)}
            except Exception:
                return 503, {"error": "source_unavailable"}
        return None

    @staticmethod
    def _redact_decision(decision: dict) -> dict:
        redacted = json.loads(json.dumps(decision, ensure_ascii=False))
        proposal = redacted.get("proposal") or {}
        action = proposal.get("action") or {}
        parameters = action.get("parameters")
        if isinstance(parameters, dict) and "text" in parameters:
            parameters["text"] = "[REDACTED]"
        return redacted

    def _read_json_body(self, handler: BaseHTTPRequestHandler) -> dict | None:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(handler, 400, {"error": "invalid_content_length"})
            return None
        if length <= 0 or length > self.max_body_bytes:
            self._send(handler, 413, {"error": "body_limit"})
            return None
        try:
            payload = json.loads(handler.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(handler, 400, {"error": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._send(handler, 400, {"error": "body_must_be_object"})
            return None
        return payload

    def _handle_proposal(self, handler: BaseHTTPRequestHandler) -> None:
        payload = self._read_json_body(handler)
        if payload is None:
            return
        risk = payload.get("risk") or {}
        if risk.get("level") in {"high", "critical"}:
            self._send(handler, 403, {"error": "high_risk_action_requires_explicit_policy_path"})
            return
        try:
            decision = agent_decisions.create_proposal(payload)
            self._send(handler, 201, {"proposal": self._redact_decision(decision)})
        except agent_decisions.AuthorizationError as exc:
            self._send(handler, 403, {"error": str(exc)})
        except agent_decisions.ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except ValueError as exc:
            self._send(handler, 400, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "proposal_persistence_unavailable"})

    def _handle_replan(self, handler: BaseHTTPRequestHandler, decision_id: str) -> None:
        decision = agent_decisions.get_decision(decision_id)
        if decision is None:
            self._send(handler, 404, {"error": "decision_not_found"})
            return
        feedback = decision.get("feedback") or []
        if not feedback:
            self._send(handler, 404, {"error": "replan_not_requested"})
            return
        self._send(handler, 200, {"replan": {
            "decision_id": decision["decision_id"],
            "parent_decision_id": decision["parent_decision_id"],
            "proposal": self._redact_decision(decision)["proposal"],
            "feedback": feedback[-1],
            "replan_request_id": decision.get("replan_request_id"),
            "attempt": decision.get("attempt", 0),
            "status": decision.get("status"),
        }})

    def _sessions(self) -> list[dict]:
        try:
            sessions = self.pty_manager.get_all_sessions()
        except Exception:
            return []
        return [{
            "session_key": item.get("session_key"),
            "group_name": item.get("group_name"),
            "terminal_type": item.get("terminal_type"),
            "is_alive": bool(item.get("is_alive")),
            "state": item.get("state"),
            "state_ts": item.get("state_ts"),
        } for item in sessions if item.get("session_key")]

    def _work_items(self) -> list[dict]:
        try:
            store = self.work_item_store.load_store()
        except Exception:
            return []
        allowed = {
            "id", "source", "external_key", "external_url", "title", "status", "duedate",
            "duedate_has_time", "person", "sort_order", "done", "archived", "workflow_state",
            "waiting_since", "created_at", "closed_at",
        }
        return [{key: value for key, value in item.items() if key in allowed}
                for item in store.get("items", []) if isinstance(item, dict)]

    def _send(self, handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status, encoded = 503, b'{"error":"response_encoding_failed"}'
        if len(encoded) > self.max_response_bytes:
            status, encoded = 503, b'{"error":"response_limit"}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        try:
            handler.wfile.write(encoded)
        except OSError:
            pass
