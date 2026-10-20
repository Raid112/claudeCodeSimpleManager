import type { EngineInterface, Register } from 'claude-code'

import { connect, isConnected, send } from './client'

// Translates native engine events into the hook vocabulary terminal/hook_state.py
// already speaks (plan Docs/plans/2026-10-19-ClaudeManagerMod.md, section 4).
// classic.* hooks do not fire on an org-managed install, so none are used here.
// Every hook calls next(e) and changes nothing: the mod only reports.

const PLUGIN_VERSION = '0.1.0'
const USAGE_EVERY_MS = 5000
// tool.check runs before the mode settles an ask: under auto / acceptEdits the
// classifier or the mode often lets the call run with no dialog. So an `ask` is
// reported only if the call is still held this long after it; one the mode settles
// sooner never flashes `tooluse` (no composer block, no chime, no Hermes wake-up).
// The classifier often holds a call far longer than this, so the manager applies
// the ask only once the tab has drawn a dialog (PtyManager.on_event).
const ASK_GRACE_MS = 3000

let sessionId = ''
let lastUsageAt = 0
const pendingAsks = new Map<string, { cancel: () => void }>()

function settleAsk(toolUseId: string | undefined): void {
  if (!toolUseId) return
  pendingAsks.get(toolUseId)?.cancel()
  pendingAsks.delete(toolUseId)
}

async function refreshSessionId($: EngineInterface): Promise<void> {
  try {
    sessionId = await $.session.id()
  } catch {
    // keep the last known id
  }
}

function event($: EngineInterface, name: string, fields: Record<string, unknown> = {}): void {
  if (!sessionId) return
  void send($, '/v1/event', { event: name, session_id: sessionId, ...fields })
}

async function reportUsage($: EngineInterface, force: boolean): Promise<void> {
  const now = Date.now()
  if (!force && now - lastUsageAt < USAGE_EVERY_MS) return
  lastUsageAt = now
  try {
    const usage = await $.session.usage()
    void send($, '/v1/usage', {
      context: usage.context,
      rateLimits: usage.rateLimits,
      cost: usage.cost,
      startedAt: usage.startedAt,
    })
  } catch {
    // usage is best effort
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    const url = await $.env.get('CLAUDEMANAGER_BRIDGE_URL')
    const token = await $.env.get('CLAUDEMANAGER_BRIDGE_TOKEN')
    // Outside a ClaudeManager tab the env carries neither: stay inert.
    connect(url && token ? { url, token } : null)
    if (!isConnected()) return result
    await refreshSessionId($)
    const { version } = await $.session.version()
    void send($, '/v1/hello', {
      session_id: sessionId,
      plugin_version: PLUGIN_VERSION,
      engine_version: version,
      cwd: e.cwd,
    })
    return result
  })

  on('turn.start', async ($, e, next) => {
    if (isConnected() && e.agentId === undefined) {
      // /clear swaps the session id without a new session.start (spike S3): read it
      // at every main-loop turn so the manager follows the swap.
      await refreshSessionId($)
      event($, 'UserPromptSubmit')
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (!isConnected()) return next(e)
    const fields = { tool_name: e.tool, tool_use_id: e.tool_use_id, agent_id: e.agentId }
    event($, 'PreToolUse', fields)
    try {
      return await next(e)
    } finally {
      settleAsk(e.tool_use_id)
      event($, 'PostToolUse', fields)
    }
  }).catch(($, e, next) => next(e)) // fail open: a broken report never blocks a tool

  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    // A query ($.tool.check) has no tool_use_id and opens no dialog.
    const id = e.tool_use_id
    if (isConnected() && verdict.decision === 'ask' && id && !pendingAsks.has(id)) {
      const fields = { tool_name: e.tool, tool_use_id: id, agent_id: e.agentId }
      pendingAsks.set(id, $.clock.after(ASK_GRACE_MS, () => {
        pendingAsks.delete(id)
        event($, 'PermissionAsk', fields)
      }))
    }
    return verdict
  }).catch(($, e, next) => next(e)) // fail open: the engine's own verdict stands

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (isConnected() && e.agentId === undefined) {
      for (const id of [...pendingAsks.keys()]) settleAsk(id)
      if (e.reason === 'error' || e.reason === 'refusal') {
        event($, 'StopFailure', { error: true })
      } else {
        event($, 'Stop', { aborted: e.reason === 'aborted' })
      }
      await reportUsage($, true)
    }
    return result
  })

  on('session.measure', async ($, e, next) => {
    const result = await next(e)
    if (isConnected()) await reportUsage($, false)
    return result
  })

  on('session.end', async ($, e, next) => {
    if (isConnected()) event($, 'SessionEnd')
    return next(e)
  })
}
