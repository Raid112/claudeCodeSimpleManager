import type { EngineInterface, Register } from 'claude-code'

// Throwaway probe v2 for Docs/plans/2026-10-19-ClaudeManagerMod.md (Fase 0).
// classic.* never fires on this org-managed install, so only native events here.
// tool.check is the one hook that changes behaviour: on an `ask` it asks the
// spike server what to answer (S4).

let base: string | undefined
let seq = 0

async function post($: EngineInterface, path: string, body: Record<string, unknown>): Promise<any> {
  if (!base) return undefined
  try {
    const r = await $.http.fetch(`${base}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-cm-tab-token': 'spike' },
      body: JSON.stringify({ seq: ++seq, t: Date.now(), ...body }),
    })
    return r.text ? JSON.parse(r.text) : undefined
  } catch (err) {
    return { fetchError: String(err) }
  }
}

function startInbox($: EngineInterface) {
  let ticks = 0
  const tick = async () => {
    let got = 0
    try {
      const t1 = Date.now()
      const r = await $.http.fetch(`${base}/inbox?wait=20`, { headers: { 'x-cm-tab-token': 'spike' } })
      const items: { id: string; text: string }[] = JSON.parse(r.text).items ?? []
      got = items.length
      ticks++
      if (ticks % 3 === 0 || got) await post($, '/log', { what: 'inbox.tick', ticks, waitedMs: Date.now() - t1, got })
      for (const it of items) {
        $.clock.after(0, async () => {
          const t2 = Date.now()
          await post($, '/ack', { id: it.id, state: 'claimed' })
          try {
            const res = await $.prompt.submit({ text: it.text })
            await post($, '/ack', { id: it.id, state: 'started', res, ms: Date.now() - t2 })
          } catch (err) {
            await post($, '/ack', { id: it.id, state: 'failed', err: String(err) })
          }
        })
      }
    } catch (err) {
      await post($, '/log', { what: 'inbox.error', err: String(err) })
    }
    $.clock.after(got ? 0 : 200, tick)
  }
  $.clock.after(0, tick)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    base = await $.env.get('CM_SPIKE_URL')
    const tab = await $.env.get('CLAUDEMANAGER_TAB')
    const dirs = await $.env.get('CLAUDE_CODE_PLUGIN_DIRS')
    const version = await $.session.version()
    const session_id = await $.session.id()
    await post($, '/hello', { tab, dirs, version, session_id, cwd: e.cwd, isInteractive: e.isInteractive, surface: e.surface })
    if (base) startInbox($)
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    const result = await next(e)
    await post($, '/event', {
      event: 'prompt.submit', text: e.text, origin: (e as any).origin, isMeta: (e as any).isMeta,
      command: (e as any).command, session_id: await $.session.id(), result,
    })
    return result
  })

  on('turn.start', async ($, e, next) => {
    await post($, '/event', { event: 'turn.start', turnId: e.turnId, agentId: e.agentId, textLen: e.text.length, session_id: await $.session.id() })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    await post($, '/event', {
      event: 'turn.complete', reason: e.reason, agentId: (e as any).agentId, text: result.text?.slice(0, 500),
      session_id: await $.session.id(),
    })
    await post($, '/usage', { from: 'turn.complete', usage: await $.session.usage() })
    return result
  })

  on('tool.call', async ($, e, next) => {
    const t1 = Date.now()
    await post($, '/event', { event: 'tool.call.pre', tool: e.tool, tool_use_id: (e as any).tool_use_id, agentId: (e as any).agentId })
    const r = await next(e)
    await post($, '/event', { event: 'tool.call.post', tool: e.tool, tool_use_id: (e as any).tool_use_id, ms: Date.now() - t1, isError: (r as any).isError, deny: (r as any).deny })
    return r
  })

  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    await post($, '/check', { tool: e.tool, tool_use_id: e.tool_use_id, agentId: e.agentId, verdict })
    if (verdict.decision !== 'ask' || !e.tool_use_id) return verdict
    const t1 = Date.now()
    next.signal.addEventListener('abort', () => {
      void post($, '/log', { what: 'permission.aborted', afterMs: Date.now() - t1 })
    })
    const answer = await post($, '/permission', { tool: e.tool, tool_use_id: e.tool_use_id })
    await post($, '/log', { what: 'permission.answer', answer, afterMs: Date.now() - t1, aborted: next.signal.aborted })
    if (answer?.behavior === 'allow') return { decision: 'allow', reason: 'spike' }
    if (answer?.behavior === 'deny') return { decision: 'deny', reason: 'denied by spike' }
    return verdict
  })

  on('session.measure', async ($, e, next) => {
    await post($, '/usage', { from: 'session.measure', rateLimits: (e as any).rateLimits })
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await post($, '/event', { event: 'session.end', e })
    return next(e)
  })
}
