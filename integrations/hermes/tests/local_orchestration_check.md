# Local Hermes orchestration smoke test

This is a manual checklist. It is intentionally separate from the Python unit
checks because it requires a fresh Windows ClaudeManager process and the native
Hermes interfaces.

## Automated evidence

Run from the repository root:

```powershell
python tests/agent_contracts_check.py
python tests/agent_decisions_check.py
python tests/agent_gateway_check.py
python tests/approval_flow_check.py
python integrations/hermes/tests/mcp_server_check.py
python -m compileall -q terminal api integrations main.py
node --check web/js/app.js
node --check web/js/agent-approvals.js
git diff --check
```

Record the date and output in the handoff. Passing these commands does not
prove that Hermes CLI or Desktop started with the MCP configuration.

## Preconditions

- [ ] ClaudeManager was restarted after backend changes.
- [ ] Gateway is on `127.0.0.1` and its token is supplied without printing it.
- [ ] CLI and Desktop use the same `HERMES_HOME`.
- [ ] `/model` reports `openai-codex/gpt-5.6-luna`.
- [ ] `/status` reports the expected profile and MCP runtime.

## CLI/TUI smoke test

Start `hermes chat` and record evidence separately:

- [ ] `/tools` shows `mcp_claudemanager_get_decision`,
  `mcp_claudemanager_open_session`, and
  `mcp_claudemanager_send_prompt`.
- [ ] A read request returns `session_key`, state, and redacted context only.
- [ ] A proposed action appears in the ClaudeManager approval UI.
- [ ] Before approval, execution is denied.
- [ ] After approval, `get_decision` reports `approved` and the exact action
  returns a host-side receipt.
- [ ] Changing the text, session key, action type, or proposal hash is rejected
  and does not write to the PTY.
- [ ] Rejecting a proposal returns `get_replan` as MCP `isError=true` and a new
  proposal carries `parent_decision_id`.
- [ ] Hermes cannot approve, emergency-stop, access WebSocket, or run shell via
  the bridge.

## Desktop smoke test

Repeat the same scenario using `hermes desktop`:

- [ ] The same MCP tools are visible without a second bridge process.
- [ ] The same decision journal shows the proposal and lineage.
- [ ] Approval, exact execution, rejection, and replan behave as in CLI.
- [ ] No provider-level completion is claimed from a host receipt.

## Evidence status

- Automated checks: passed in the current checkout.
- Hermes CLI/TUI: pending manual run.
- Hermes Desktop: pending manual run.
- Remote/Docker/public network: intentionally not tested; outside MVP.
