"""Authenticated, read-only local gateway for the Hermes integration.

The gateway is intentionally a small host boundary.  It receives live dependencies by
injection, exposes only versioned context reads in this checkpoint, and never forwards a
browser WebSocket, PTY handle, filesystem path, or credential to Hermes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from terminal.agent_contracts import (
    ACTION_LINK_WORK_ITEM, ACTION_OPEN_SESSION, ACTION_SEND_OWNED_PROMPT,
)
from terminal.input_protocol import InputProtocolError
from terminal.agent_decisions import AuthorizationError, ConflictError
from terminal import agent_decisions, hermes_events
from terminal.agent_sources import SourcePoller
from terminal.work_items import resolve_work_item_id


class GatewayAuthError(RuntimeError):
    pass


# GET /v1/events (see AgentGateway._handle_events).
EVENTS_DEFAULT_WINDOW_S = 900.0  # missing `since` -> a bounded recent catch-up window,
                                  # not an unbounded replay (work_items.read_events
                                  # tolerates since=None but would then walk up to 60
                                  # days of retained partitions on every call).
EVENTS_MAX_RECORDS = 500  # keeps the response well under max_response_bytes (512KB);
                           # the live log already holds 41k+ raw hook records.


class AgentGateway:
    def __init__(self, pty_manager, work_item_store, jira_adapter, teams_adapter, *,
                 host: str = "127.0.0.1", port: int = 8787,
                 hermes_token: str | None = None, operator_token: str | None = None,
                 groups: list[dict] | None = None,
                 auto_prompt_groups: list[str] | None = None,
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
        # Groups whose Hermes-opened tabs accept prompts with no approval. Empty by
        # default: the lane stays closed until config.json names a group.
        self.auto_prompt_groups = {
            str(name) for name in (auto_prompt_groups or [])
            if str(name) in self.configured_groups
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
        # request_id -> {"lock": RLock, "refcount": int}. Refcounted so eviction
        # (see _execution_lock) can never remove an entry a thread is still
        # inside, while still not growing forever like the plain dict it replaced.
        self._execution_locks: dict[str, dict] = {}
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
                # Approval-free lane for the remote case (nobody at the PC): only a
                # tab Hermes opened itself, only in an allow-listed group. Every
                # other prompt still goes host-owned through proposal + approval.
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                if self.emergency_stopped:
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                self._handle_owned_prompt(handler, prompt_session_key)
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
            if path.path == "/v1/work-items/link" and method == "POST":
                if not is_hermes:
                    self._send(handler, 403, {"error": "hermes_read_token_required"})
                    return
                if self.emergency_stopped:
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                self._handle_link_work_item(handler)
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

    @contextlib.contextmanager
    def _execution_lock(self, request_id: str):
        """Serialize concurrent requests sharing one request_id, without leaking
        one RLock per distinct request_id forever (the previous behaviour).

        The dict entry is refcounted under _execution_locks_guard: a thread
        registers its use (increments refcount) *before* it can block on the
        actual RLock, and only decrements/evicts after it is fully done with the
        entry (lock released). That refcount -- not whether the RLock happens to
        be acquired at this instant -- is what "held" means here, so an entry can
        never be evicted out from under a thread still using it, and a
        request_id reused later (e.g. a dispatch retry's deterministic id) just
        creates a fresh entry.  Call sites are unchanged: still `with
        self._execution_lock(request_id):`.
        """
        with self._execution_locks_guard:
            entry = self._execution_locks.get(request_id)
            if entry is None:
                entry = {"lock": threading.RLock(), "refcount": 0}
                self._execution_locks[request_id] = entry
            entry["refcount"] += 1
        try:
            with entry["lock"]:
                yield
        finally:
            with self._execution_locks_guard:
                entry["refcount"] -= 1
                if entry["refcount"] == 0 and self._execution_locks.get(request_id) is entry:
                    del self._execution_locks[request_id]

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
        """`open_session` is automatic: no proposal, no approval, no decision at all.

        Only the configured-group check and the emergency stop still gate it. The
        action is journaled via ``record_automatic_action`` (audit, not authorization)
        and made idempotent per ``request_id`` so a replay or a concurrent duplicate
        never opens a second tab.
        """
        payload = self._read_json_body(handler)
        if payload is None:
            return
        required = {"request_id", "group_id"}
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
            # Keyed by request_id (not decision_id — there is no decision), so two
            # concurrent identical requests serialize here before either touches
            # the PTY manager or the journal.
            with self._execution_lock(payload["request_id"]):
                existing = agent_decisions.find_automatic_action(payload["request_id"])
                if existing is not None:
                    # find_automatic_action matches on request_id alone (Task 4 adds a
                    # second automatic route, link_work_item, sharing the same journal
                    # helper) — guard against a reused request_id replaying the wrong
                    # action's result instead of this route's own.
                    if existing.get("action_type") != ACTION_OPEN_SESSION:
                        raise ConflictError("request_id is already bound to a different automatic action")
                    self._send(handler, 201, {"session": existing.get("result")})
                    return
                root = self._safe_configured_path(group["path"])
                session = self.pty_manager.create_session(
                    group["name"], str(root), terminal_type="claude")
                public = {
                    "session_key": session.session_key,
                    "group_id": group_id,
                    "group_name": group["name"],
                    "terminal_type": "claude",
                    "state": "DISPATCHED",
                }
                # The tab exists before the journal write can name its session_key, so
                # a failed write (emergency stop flipped, audit storage error) would
                # leave an unjournaled tab AND let a replay open a second one. Undo the
                # creation instead: either the action is recorded or it did not happen.
                try:
                    agent_decisions.record_automatic_action(
                        ACTION_OPEN_SESSION, payload["request_id"], public)
                except Exception:
                    try:
                        self.pty_manager.close_session(session.id)
                    except Exception:
                        pass
                    raise
                self._send(handler, 201, {"session": public})
        except AuthorizationError:
            # Emergency stop flipped between the route-level check and this write.
            self._send(handler, 503, {"error": "emergency_stop_active"})
        except ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except ValueError as exc:
            self._send(handler, 400, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "session_creation_unavailable"})

    def _handle_owned_prompt(self, handler: BaseHTTPRequestHandler, session_key: str) -> None:
        """``send_owned_prompt`` is automatic, scoped by ownership instead of approval.

        Gates, in order: identifier shape, the tab exists, Hermes opened it (journal
        has its ``open_session``), its group is in ``auto_prompt_groups``. Idempotent
        per ``request_id`` like the other automatic routes. The journal write runs
        after the PTY write, so a journal failure is reported as 503 with
        ``written: true`` and the caller must not retry with a new request_id.
        """
        payload = self._read_json_body(handler)
        if payload is None:
            return
        required = {"request_id", "text"}
        if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
            self._send(handler, 400, {"error": "owned_prompt_contract"})
            return
        request_id, text = payload["request_id"], payload["text"]
        identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
        if not identifier.fullmatch(request_id) or not identifier.fullmatch(session_key):
            self._send(handler, 400, {"error": "invalid_identifier"})
            return
        if not text.strip():
            self._send(handler, 400, {"error": "empty_prompt"})
            return
        try:
            with self._execution_lock(request_id):
                existing = agent_decisions.find_automatic_action(request_id)
                if existing is not None:
                    if existing.get("action_type") != ACTION_SEND_OWNED_PROMPT:
                        raise ConflictError("request_id is already bound to a different automatic action")
                    self._send(handler, 201, {"prompt": existing.get("result")})
                    return
                session = self.pty_manager.find_by_session_key(session_key)
                if session is None:
                    self._send(handler, 404, {"error": "session_not_found"})
                    return
                if getattr(session, "group_name", None) not in self.auto_prompt_groups:
                    self._send(handler, 403, {"error": "group_not_enabled_for_auto_prompt"})
                    return
                if not agent_decisions.hermes_opened_session(session_key):
                    self._send(handler, 403, {"error": "session_not_opened_by_hermes"})
                    return
                try:
                    written = self.pty_manager.send_owned_prompt(session_key, text)
                except InputProtocolError as exc:
                    self._send(handler, 400, {"error": f"input_protocol: {exc}"})
                    return
                public = {
                    "session_key": session_key,
                    "state": "SENT" if written else "UNKNOWN",
                    "provider_processed": False,
                    "prompt_length": len(text),
                }
                try:
                    agent_decisions.record_automatic_action(
                        ACTION_SEND_OWNED_PROMPT, request_id, public)
                except Exception:
                    self._send(handler, 503, {"error": "prompt_journal_unavailable", "written": written})
                    return
                self._send(handler, 201 if written else 502, {"prompt": public})
        except ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except PermissionError as exc:
            self._send(handler, 403, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "owned_prompt_unavailable"})

    def _handle_link_work_item(self, handler: BaseHTTPRequestHandler) -> None:
        """``link_work_item`` is automatic: no proposal, no approval.

        Order matters and deliberately differs from ``_handle_session_create``:
        here ``work_items.link`` runs BEFORE the journal write, not after. That
        route creates a scarce resource (a PTY tab) it must undo if the journal
        write fails, or a retry opens a second one. ``work_items.link`` is
        idempotent -- relinking the same session to the same Work Item just
        bumps ``ts`` -- so a retry after a journal failure repeats a harmless
        write instead of needing an undo.
        """
        payload = self._read_json_body(handler)
        if payload is None:
            return
        required = {"request_id", "session_key", "work_item_id"}
        if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
            self._send(handler, 400, {"error": "link_work_item_contract"})
            return
        request_id = payload["request_id"]
        session_key = payload["session_key"]
        work_item_id = payload["work_item_id"]
        identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
        if not all(identifier.fullmatch(value) for value in (request_id, session_key, work_item_id)):
            self._send(handler, 400, {"error": "invalid_identifier"})
            return
        try:
            with self._execution_lock(request_id):
                existing = agent_decisions.find_automatic_action(request_id)
                if existing is not None:
                    if existing.get("action_type") != ACTION_LINK_WORK_ITEM:
                        raise ConflictError("request_id is already bound to a different automatic action")
                    self._send(handler, 201, {"link": existing.get("result")})
                    return
                session = self.pty_manager.find_by_session_key(session_key)
                if session is None:
                    self._send(handler, 404, {"error": "session_not_found"})
                    return
                try:
                    store = self.work_item_store.load_store()
                except Exception:
                    self._send(handler, 503, {"error": "work_items_unavailable"})
                    return
                items = store.get("items", []) if isinstance(store, dict) else []
                if not any(isinstance(item, dict) and item.get("id") == work_item_id for item in items):
                    self._send(handler, 404, {"error": "work_item_not_found"})
                    return
                # Mirrors app.js sessionLinkKeyOf exactly: a claude tab's durable
                # work-item key is its claude_session_id (falling back to
                # session_key only if the provider id isn't known yet); other
                # providers key by session_key itself. Writing under the wrong
                # key would create a second link the frontend never reads.
                terminal_type = getattr(session, "terminal_type", None)
                if terminal_type == "powershell":
                    link_key = None
                elif terminal_type == "claude":
                    link_key = getattr(session, "claude_session_id", None) or session_key
                else:
                    link_key = (session_key or getattr(session, "agent_session_id", None)
                                or getattr(session, "claude_session_id", None))
                if not link_key:
                    self._send(handler, 400, {"error": "session_not_linkable"})
                    return
                public = {"session_key": session_key, "work_item_id": work_item_id, "link_key": link_key}
                try:
                    self.work_item_store.link(
                        link_key, work_item_id,
                        group_name=getattr(session, "group_name", None),
                        path=getattr(session, "path", None))
                except Exception:
                    self._send(handler, 503, {"error": "link_unavailable"})
                    return
                try:
                    agent_decisions.record_automatic_action(ACTION_LINK_WORK_ITEM, request_id, public)
                except AuthorizationError:
                    # Emergency stop flipped between the route-level check and this
                    # write. The link above already happened; unlinking it back out
                    # risks clobbering a pre-existing, unrelated link for the same
                    # key (work_items.unlink removes the whole entry, it does not
                    # restore a prior value) -- narrower and safer to leave the link
                    # in place and simply not journal it than to "undo" it.
                    self._send(handler, 503, {"error": "emergency_stop_active"})
                    return
                except Exception:
                    self._send(handler, 503, {"error": "link_persistence_unavailable"})
                    return
                self._send(handler, 201, {"link": public})
        except ConflictError as exc:
            self._send(handler, 409, {"error": str(exc)})
        except Exception:
            self._send(handler, 503, {"error": "link_unavailable"})

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
                "read_only": not self.auto_prompt_groups,
                "auto_prompt_groups": sorted(self.auto_prompt_groups),
            }
        if path == "/v1/sessions":
            return 200, {"sessions": self._sessions()}
        if path == "/v1/events":
            return self._handle_events(query)
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

    def _handle_events(self, query: dict[str, list[str]]) -> tuple[int, dict]:
        """``GET /v1/events?since=<unix ts>`` -- the reconciliation view of what
        the webhook publisher (``terminal/hermes_events.HermesEventsPublisher``)
        would have sent. Not a second event queue: reads the same append-only
        hook-event log via ``work_items.read_events`` and runs every record
        through ``hermes_events.normalize_event``, the identical pure mapping
        the publisher uses, so the two paths can never disagree on what counts
        as a wake-up.

        Only the three publishable types are returned (``normalize_event``
        returning ``None`` means logged-only -- already on disk via
        ``work_items``, nothing to reconcile). Mixing that raw hook noise into
        this response would make a future reconciliation job double-count
        events the publisher never would have sent in the first place.

        Not gated by the emergency stop: every other GET route
        (``/v1/sessions``, ``/v1/work-items``, ``/v1/work-overview``, ...)
        stays readable while stopped too. The stop is a kill-switch for
        autonomous WRITES (``/v1/sessions`` POST, ``/v1/work-items/link``
        POST, and the always-403 prompt route), not for reading already-logged,
        side-effect-free metadata.

        ``since`` is a float unix timestamp, matching ``work_items.read_events``.
        A missing ``since`` defaults to ``EVENTS_DEFAULT_WINDOW_S`` seconds
        before now rather than an unbounded replay. A malformed ``since`` is a
        400, not a silent fallback.

        The response is capped at ``EVENTS_MAX_RECORDS`` publishable events
        (well under ``max_response_bytes`` -- the live log already holds 41k+
        raw records) and always carries ``next_since`` plus ``truncated`` so a
        caller pages instead of assuming one call drains the log. When a page
        is NOT truncated, ``next_since`` is the query time captured before the
        read (or the newest record actually scanned, if that is later) --
        never just the last event returned -- so a window with zero
        publishable events (e.g. only PreToolUse/PostToolUse activity) still
        advances instead of being re-read forever. A truncated page instead
        anchors ``next_since`` to its last returned event's timestamp so the
        caller resumes exactly where the cap cut off; per work_items' own
        cursor-resume note, several records can share one float `ts`, so a
        caller must dedup by `event_id` across pages -- the same guarantee the
        publisher's cursor already relies on.
        """
        raw_since = (query.get("since") or [None])[0]
        query_ts = time.time()
        if raw_since is None:
            since = query_ts - EVENTS_DEFAULT_WINDOW_S
        else:
            try:
                since = float(raw_since)
            except (TypeError, ValueError):
                return 400, {"error": "invalid_since"}
            if since != since or since in (float("inf"), float("-inf")) or since < 0:
                return 400, {"error": "invalid_since"}

        # Widen the read slightly for disambiguation context only (mirrors the
        # publisher's own OVERLAP_WINDOW_S use): a Notification landing right at
        # the `since` boundary needs its preceding same-session raw event to
        # tell permission_requested apart from a logged-only status_changed.
        # Never surfaced itself -- filtered back out by the `ts < since` check.
        lookback = max(0.0, since - hermes_events.OVERLAP_WINDOW_S)
        try:
            records = self.work_item_store.read_events(since=lookback)
        except Exception:
            return 503, {"error": "events_unavailable"}
        try:
            session_links = self.work_item_store.load_store().get("session_links", {})
        except Exception:
            session_links = {}

        prev_event_by_session: dict[str, str] = {}
        scanned_max_ts = since
        normalized_events: list[tuple[float, dict]] = []
        for rec in records:  # work_items.read_events returns ts-ascending
            ts = rec.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            session_key = rec.get("session_key")
            prev_event = prev_event_by_session.get(session_key) if session_key else None
            try:
                normalized = hermes_events.normalize_event(rec, prev_event)
            except Exception:
                normalized = None
            # Update ordering context for every in-window-or-lookback record,
            # even one that turns out logged-only, or the next record for this
            # session would be disambiguated against a stale prev_event.
            if session_key:
                prev_event_by_session[session_key] = rec.get("event")
            if ts < since:
                continue  # context-only: seeded prev_event above, never surfaced
            if ts > scanned_max_ts:
                scanned_max_ts = ts
            if normalized is None:
                continue
            normalized["work_item_id"] = resolve_work_item_id(
                session_links, session_key, rec.get("claude_session_id"))
            normalized_events.append((float(ts), normalized))

        normalized_events.sort(key=lambda pair: pair[0])
        truncated = len(normalized_events) > EVENTS_MAX_RECORDS
        page = normalized_events[:EVENTS_MAX_RECORDS]
        next_since = page[-1][0] if truncated else max(scanned_max_ts, query_ts)
        return 200, {
            "events": [item for _, item in page],
            "next_since": next_since,
            "truncated": truncated,
            "cap": EVENTS_MAX_RECORDS,
        }

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
            "duedate_has_time", "person", "sort_order", "done", "workflow_state",
            "waiting_since", "woke_at", "woke_by", "created_at", "closed_at",
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
