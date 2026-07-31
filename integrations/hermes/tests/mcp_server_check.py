import copy
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from integrations.hermes.mcp_server import McpServer, RuntimeAttestation, McpProtocolError


def attestation():
    return RuntimeAttestation(
        hermes_profile="claudemanager",
        hermes_version="0.19.0",
        provider="openai-codex",
        model="gpt-5.6-luna",
        config_hash="a" * 64,
        session_id="runtime-1",
    )


def test_tool_surface_is_narrow_and_protocol_errors_are_distinct():
    server = McpServer("http://127.0.0.1:1", "token", attestation(), request_fn=lambda *args: (200, {}))
    names = {tool["name"] for tool in server.tools()}
    assert "list_sessions" in names and "submit_proposal" in names
    assert "approve" not in names and "execute" not in names and "raw_websocket" not in names
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert "result" in result
    try:
        server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
    except McpProtocolError:
        pass
    else:
        raise AssertionError("malformed MCP call must be a protocol error")


def test_business_rejection_is_mcp_is_error_and_second_cycle_can_submit():
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if len(calls) == 1:
            return 403, {"error": "wrong_scope", "decision_id": "v1"}
        return 201, {"proposal": {"decision_id": "v2", "status": "awaiting_approval"}}

    server = McpServer("http://127.0.0.1:1", "token", attestation(), request_fn=request)
    first = {"decision_id": "v1", "model": {"provider": "openai-codex", "name": "gpt-5.6-luna",
             "attestation": {"hermes_profile": "x", "hermes_version": "x", "config_hash": "b" * 64,
                              "session_id": "x"}}}
    rejected = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "submit_proposal", "arguments": {"proposal": first}}})
    assert rejected["result"]["isError"] is True
    assert "decision_id" in rejected["result"]["structuredContent"]
    second = copy.deepcopy(first)
    second["decision_id"] = "v2"
    accepted = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "submit_proposal", "arguments": {"proposal": second}}})
    assert accepted["result"]["isError"] is False
    assert len(calls) == 2
    replan = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "get_replan", "arguments": {"decision_id": "v1"}}})
    assert replan["result"]["isError"] is True


def test_attestation_must_be_effective_luna():
    try:
        RuntimeAttestation("profile", "0.1", "other", "auto", "a" * 64, "s").verify()
    except ValueError:
        pass
    else:
        raise AssertionError("alternate model must be denied")


def test_execution_tools_have_strict_contracts_and_call_semantic_routes():
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.startswith("/v1/proposals/"):
            return 200, {"decision": {"decision_id": "d-1", "status": "approved"}}
        if path == "/v1/sessions":
            return 201, {"session": {"session_key": "new-session", "state": "DISPATCHED"}}
        return 200, {"receipt": {"state": "SENT", "result": "host_write_accepted"}}

    server = McpServer("http://127.0.0.1:1", "token", attestation(), request_fn=request)
    tools = {tool["name"]: tool for tool in server.tools()}
    for name, fields in {
        "get_decision": {"decision_id"},
        "open_session": {"decision_id", "proposal_hash", "request_id", "group_id"},
        "send_prompt": {"decision_id", "proposal_hash", "request_id", "session_key", "text"},
    }.items():
        schema = tools[name]["inputSchema"]
        assert set(schema["required"]) == fields
        assert schema.get("additionalProperties") is False

    def call(name, arguments):
        return server.handle({"jsonrpc": "2.0", "id": name, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})

    assert call("get_decision", {"decision_id": "d-1"})["result"]["isError"] is False
    assert call("open_session", {"decision_id": "d-1", "proposal_hash": "a" * 64,
                                  "request_id": "r-1", "group_id": "group-1"})["result"]["isError"] is False
    assert call("send_prompt", {"decision_id": "d-1", "proposal_hash": "a" * 64,
                                 "request_id": "r-2", "session_key": "session-1",
                                 "text": "approved"})["result"]["isError"] is False
    assert calls == [
        ("GET", "/v1/proposals/d-1", None),
        ("POST", "/v1/sessions", {"decision_id": "d-1", "proposal_hash": "a" * 64,
                                    "request_id": "r-1", "group_id": "group-1"}),
        ("POST", "/v1/sessions/session-1/prompt", {"decision_id": "d-1",
                                                     "proposal_hash": "a" * 64,
                                                     "request_id": "r-2", "text": "approved"}),
    ]
    try:
        call("send_prompt", {"decision_id": "d-1", "proposal_hash": "a" * 64,
                              "request_id": "r-3", "session_key": "session-1",
                              "text": "ok", "extra": True})
    except McpProtocolError:
        pass
    else:
        raise AssertionError("execution tools must reject unknown arguments")


def test_execution_errors_and_unknown_receipts_are_visible_without_sensitive_fields():
    def error_request(method, path, payload=None):
        return 503, {"error": "gateway_unavailable", "token": "secret", "path": r"C:\private"}

    server = McpServer("http://127.0.0.1:1", "token", attestation(), request_fn=error_request)
    result = server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                            "params": {"name": "send_prompt", "arguments": {
                                "decision_id": "d-1", "proposal_hash": "a" * 64,
                                "request_id": "r-1", "session_key": "session-1", "text": "x"}}})
    assert result["result"]["isError"] is True
    encoded = json.dumps(result)
    assert "secret" not in encoded and "C:\\private" not in encoded

    server = McpServer("http://127.0.0.1:1", "token", attestation(),
                       request_fn=lambda *args: (200, {"receipt": {"state": "UNKNOWN"}}))
    result = server.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                            "params": {"name": "send_prompt", "arguments": {
                                "decision_id": "d-1", "proposal_hash": "a" * 64,
                                "request_id": "r-1", "session_key": "session-1", "text": "x"}}})
    assert result["result"]["isError"] is True


def test_adapter_runs_as_documented_direct_script():
    env = os.environ.copy()
    env.update({
        "CLAUDEMANAGER_HERMES_TOKEN": "test-token-not-a-secret-1234567890",
        "HERMES_PROFILE": "claudemanager-test", "HERMES_VERSION": "0.19.0",
        "HERMES_PROVIDER": "openai-codex", "HERMES_MODEL": "gpt-5.6-luna",
        "HERMES_CONFIG_HASH": "a" * 64, "HERMES_SESSION_ID": "stdio-test",
    })
    process = subprocess.run(
        [sys.executable, str(ROOT / "integrations" / "hermes" / "mcp_server.py")],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
              '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n',
        text=True, capture_output=True, cwd=str(ROOT.parent), env=env, timeout=10,
    )
    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "claudemanager-hermes-bridge"
    assert "get_decision" in {tool["name"] for tool in responses[1]["result"]["tools"]}


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} MCP adapter checks passed")
