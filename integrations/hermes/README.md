# Hermes ClaudeManager Bridge

This is the host-only integration for native Hermes on Windows. Hermes CLI and
Hermes Desktop use the same `HERMES_HOME` configuration and start the same
stdio adapter. The adapter calls only the authenticated loopback gateway and
does not expose REST, PTY, WebSocket, PowerShell, filesystem, or operator
capabilities to the model.

## Prerequisites

- Windows 10/11 with Python 3.10+ on `PATH`.
- ClaudeManager dependencies installed with `pip install -r requirements.txt`.
- Hermes CLI/Desktop installed and authenticated with the approved
  `openai-codex/gpt-5.6-luna` runtime.
- A configured ClaudeManager group with an existing local directory.

No new Python dependency is required: the adapter uses Python standard-library
JSON-RPC stdio and HTTP.

## Configure once for CLI and Desktop

1. Start ClaudeManager from the repository root.
2. Copy `config.example.yaml` into the shared Hermes configuration under
   `HERMES_HOME`.
3. Replace the example adapter path with an absolute Windows path, for example
   `C:/Users/caioc/ClaudeManager/integrations/hermes/mcp_server.py`.
4. Keep `mcp_servers.claudemanager.command`, `args`, and `env` together. The
   environment must contain the loopback URL, the Hermes token, and the six
   attestation values shown in the example.
5. Supply `CLAUDEMANAGER_HERMES_TOKEN` from the process environment or use
   `CLAUDEMANAGER_HERMES_TOKEN_FILE` pointing to a protected local file. Never
   put a literal token in YAML, source, logs, or a proposal.
6. Calculate `HERMES_CONFIG_HASH` from the effective non-secret runtime settings
   using the PowerShell snippet in `claudemanager-orchestrator.md`. Do not hash
   or print credentials.
7. Use the same `HERMES_HOME` for both interfaces:

   ```powershell
   hermes chat
   hermes desktop
   ```

The adapter fails closed if the provider/model is not exactly
`openai-codex/gpt-5.6-luna`, if the attestation is incomplete, or if the gateway
token is missing.

## Available MCP tools

- `get_health`, `list_sessions`, `list_work_items`, `get_work_overview` and
  read-only source tools provide redacted context.
- `submit_proposal` creates an immutable approval request.
- `get_decision` reads redacted decision state after the local UI acts.
- `open_session` executes automatically, no proposal or approval required.
- `get_execution_receipt` reads the host's dispatch outcome for an approved
  `send_prompt` decision. There is no `send_prompt` tool: the host dispatches
  the approved prompt itself at approval time, so Hermes can only observe the
  result, never trigger the write.
- `get_replan` returns rejection feedback as `isError=true` so Hermes reasons
  again and submits a versioned child proposal.

The host verifies `decision_id`, `proposal_hash`, action type, target, exact
prompt text, expiry, approval status, and request idempotency before writing or
creating a tab. A receipt is host-side evidence only: `HOST_WRITE_ACCEPTED`
does not claim provider completion, and `PROVIDER_PROCESSING_UNKNOWN` remains
visible.

## Diagnostics

Inside either Hermes interface:

- `/tools` should list the `mcp_claudemanager_*` tools.
- `/reload-mcp` reloads the adapter after configuration edits.
- `/model` should show `openai-codex/gpt-5.6-luna`.
- `/status` should show the expected profile and MCP server.

If tools are missing, check the absolute Python path, `HERMES_HOME`, and the
gateway process. Run the adapter directly only as a process/configuration
diagnostic; a standalone adapter does not prove CLI or Desktop integration.

## Rotation and shutdown

Rotate the Hermes token in the local process configuration and restart
ClaudeManager; the gateway revokes the previous token. Update the protected
token file or environment without printing its contents, then restart both
Hermes interfaces or use `/reload-mcp`. Stop Hermes normally, then close
ClaudeManager so the gateway and WebSocket server shut down with the desktop
app. No deploy, public listener, or remote host is involved.

## Wake-up path (webhook publisher) setup

ClaudeManager tails its own hook-event log and POSTs normalized wake-up events
(`task_completed`, `permission_requested`, `process_exited`) to Hermes' native
webhook platform, HMAC-signed over loopback. This starts a **new** Hermes turn;
it never writes a prompt itself (only host-owned dispatch on approval does that,
see above).

1. **Enable the webhook platform via `.env`, not `config.yaml`** (keeps the
   secret out of plaintext YAML):

   ```text
   # %LOCALAPPDATA%\hermes\.env
   WEBHOOK_ENABLED=true
   WEBHOOK_PORT=8644
   WEBHOOK_SECRET=<same HMAC secret as below>
   ```

2. **ClaudeManager side:** put the same secret in
   `%LOCALAPPDATA%\ClaudeManager\secrets.json`:

   ```json
   { "hermes_webhook": { "secret": "<same HMAC secret as above>" } }
   ```

   No secret configured -> the publisher stays off (fail-closed); the rest of
   the app is unaffected.

3. **Define the route statically in `config.yaml`** under
   `platforms.webhook.extra.routes` (a static route cannot be overwritten by
   `hermes webhook subscribe`, and only a config route supports declarative
   `filters`):

   ```yaml
   platforms:
     webhook:
       extra:
         routes:
           claudemanager-events:
             prompt: >
               Evento do ClaudeManager: {event_type} na sessão {session_key}
               (work item {work_item_id}, status {status}, {ts}). Leia o
               contexto com list_sessions/list_work_items antes de agir -- o
               evento pode estar velho. Não assuma aprovação: qualquer prompt
               exige submit_proposal e aprovação humana na UI.
             script: claudemanager_event_filter.py
             filters:
               - field: payload.event_type
                 in: [task_completed, permission_requested, process_exited]
               - field: payload.hermes_initiated
                 equals: false
   ```

   No `secret:` key on the route -- it inherits the global `.env` secret above.

4. **Copy the filter script** from
   `integrations/hermes/claudemanager_event_filter.py` (the review copy kept
   in this repo) to `HERMES_HOME/scripts/claudemanager_event_filter.py`. Path
   traversal outside `HERMES_HOME/scripts/` is blocked, so it cannot live
   anywhere else. It only dedups by `event_id` and applies a per-session
   cooldown -- event-type selection and the recursion guard already live in
   the declarative `filters` above.

5. **Gateway durability on Windows:** `hermes gateway install` creates a real
   Scheduled Task (`schtasks /Create ... /TN Hermes_Gateway`), logon trigger,
   `LeastPrivilege`, **no UAC/elevation required**. Prefer it over running
   `hermes gateway run` in a foreground console.

6. **`hermes webhook test` cannot exercise this route.** It only works against
   a *dynamic* subscription (reads its secret from
   `webhook_subscriptions.json`) and signs in the GitHub format
   (`X-Hub-Signature-256`), not the generic V2 format this route uses. There is
   no CLI round-trip for a static route; the publisher's own delivery *is* the
   test.

With the gateway down, events simply accumulate in ClaudeManager's local log
(nothing is dropped) and the publisher backs off; delivery resumes
automatically once the gateway is back up, with no ClaudeManager restart
needed.

## Acceptance evidence

Automated checks are kept separate from manual application evidence. The local
checklist in `claudemanager-orchestrator.md` has separate CLI/TUI and Desktop
sections. Until each is exercised after a fresh ClaudeManager restart, report
that interface as not manually validated.

## Scope and limitations

The attestation is declared by the adapter process environment; it is not an
independent proof of the parent Hermes executable. A future trusted launcher
can strengthen this without changing proposal hashes or approval semantics.

WSL/VPS, Telegram/Discord/Slack/WhatsApp, Docker as the main path, public
network exposure, arbitrary shell/filesystem access, Jira/Teams mutations,
deploys, deletes, and configuration mutation are outside this MVP. A remote
deployment would require authenticated MCP HTTP over a private network and a
new threat model.
