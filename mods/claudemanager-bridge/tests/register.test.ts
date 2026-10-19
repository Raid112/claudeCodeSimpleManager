import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const URL_ = 'http://127.0.0.1:9'
const ENV = { CLAUDEMANAGER_BRIDGE_URL: URL_, CLAUDEMANAGER_BRIDGE_TOKEN: 'tok-1' }

type Post = { path: string; token: string | undefined; body: any }

// The engine beneath the plugin: the session's nouns answered from memory and
// every $.http.fetch recorded instead of sent.
function world(on: On, env: Record<string, string>, opts: { askHoldMs?: number; engine?: any } = {}) {
  const posts: Post[] = []
  const clock = mock.clock(on)
  mock.env(on, env)
  let sid = 'sid-1'
  on('http.fetch', ($, e) => {
    const headers = (e.init?.headers ?? {}) as Record<string, string>
    posts.push({ path: e.url.replace(URL_, ''), token: headers['x-cm-tab-token'], body: JSON.parse(String(e.init?.body ?? '{}')) })
    return { value: { status: 200, ok: true, headers: {}, text: '{"ok":true}' } }
  })
  on('session.id', () => ({ value: sid }))
  on('session.version', () => ({ value: { version: '2.1.303', base: '2.1.303', builtAt: '2026-10-18T20:20:57Z' } }))
  on('session.usage', () => ({ value: { startedAt: 1, context: { tokens: 10, window: 100, percent: 10 }, rateLimits: [] } }) as any)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('tool.check', ($, e) => ({ decision: ((e.input as any)?.verdict ?? 'allow') as any }))
  // With askHoldMs, a tool call behaves as in a session: the permission check runs
  // inside tool.call's core and answers `ask`, then the call is held (a dialog
  // open) for askHoldMs before it resolves.
  on('tool.call', async (_$, e) => {
    if (opts.askHoldMs !== undefined && opts.engine) {
      await opts.engine.tool.check({ tool: e.tool, input: { verdict: 'ask' }, tool_use_id: (e as any).tool_use_id } as any)
      if (opts.askHoldMs) await clock.advance(opts.askHoldMs)
    }
    return { result: { text: 'done' } } as any
  })
  on('session.end', () => ({ sessionId: sid }) as any)
  return { posts, clock, swapSession: (next: string) => { sid = next } }
}

const START = { cwd: 'C:/work', surface: 'terminal', isInteractive: true } as const
const turnEnd = (reason: string, extra: Record<string, unknown> = {}) =>
  ({ answer: 'ok', durationMs: 5, isAborted: reason === 'aborted', turnId: 't1', reason, ...extra }) as any

const events = (posts: Post[]) => posts.filter(p => p.path === '/v1/event').map(p => p.body.event)

test('outside a ClaudeManager tab the mod sends nothing', async ($, on) => {
  const { posts, clock } = world(on, {})
  await $.session.start(START)
  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.turn.complete(turnEnd('answer'))
  await clock.settle()
  expect(posts).toEqual([])
})

test('hello carries the session id, the engine build and the tab token', async ($, on) => {
  const { posts, clock } = world(on, ENV)
  await $.session.start(START)
  await clock.settle()
  expect(posts[0]?.path).toBe('/v1/hello')
  expect(posts[0]?.token).toBe('tok-1')
  expect(posts[0]?.body).toEqual(expect.objectContaining({ session_id: 'sid-1', engine_version: '2.1.303', cwd: 'C:/work' }))
})

test('an ask still held after the grace (a real dialog) is a PermissionAsk inside the tool call', async ($, on) => {
  const w = world(on, ENV, { askHoldMs: 4000, engine: $ })
  await $.session.start(START)
  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls', tool_use_id: 'tu-1' } as any)
  await $.turn.complete(turnEnd('answer'))
  await w.clock.settle()
  expect(events(w.posts)).toEqual(['UserPromptSubmit', 'PreToolUse', 'PermissionAsk', 'PostToolUse', 'Stop'])
  const seqs = w.posts.map(p => p.body.seq)
  expect(seqs).toEqual([...seqs].sort((a, b) => a - b))
  expect(w.posts.some(p => p.path === '/v1/usage')).toBe(true)
})

test('an ask the mode settles within the grace (auto classifier) is never reported', async ($, on) => {
  const w = world(on, ENV, { askHoldMs: 0, engine: $ })
  await $.session.start(START)
  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.tool.call({ tool: 'Bash', command: 'ls', tool_use_id: 'tu-1' } as any)
  await w.clock.advance(10000)
  expect(events(w.posts)).toEqual(['UserPromptSubmit', 'PreToolUse', 'PostToolUse'])
})

test('a query check and an allow verdict report no permission ask', async ($, on) => {
  const { posts, clock } = world(on, ENV)
  await $.session.start(START)
  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.tool.check({ tool: 'Bash', input: { verdict: 'ask' } } as any)
  await $.tool.check({ tool: 'Bash', input: { verdict: 'allow' }, tool_use_id: 'tu-2' } as any)
  await clock.advance(10000)
  expect(events(posts)).toEqual(['UserPromptSubmit'])
})

test('an interrupted turn is a Stop with aborted, an API error a StopFailure', async ($, on) => {
  const { posts, clock } = world(on, ENV)
  await $.session.start(START)
  await $.turn.start({ text: 'a', turnId: 't1' })
  await $.turn.complete(turnEnd('aborted'))
  await $.turn.start({ text: 'b', turnId: 't2' })
  await $.turn.complete(turnEnd('error'))
  await clock.settle()
  const ev = posts.filter(p => p.path === '/v1/event').map(p => p.body)
  expect(ev[1]).toEqual(expect.objectContaining({ event: 'Stop', aborted: true }))
  expect(ev[3]).toEqual(expect.objectContaining({ event: 'StopFailure', error: true }))
})

test('subagent turns are not reported as the tab finishing', async ($, on) => {
  const { posts, clock } = world(on, ENV)
  await $.session.start(START)
  await $.turn.start({ text: 'sub', turnId: 's1', agentId: 'agent-1' })
  await $.turn.complete(turnEnd('answer', { turnId: 's1', agentId: 'agent-1' }))
  await clock.settle()
  expect(events(posts)).toEqual([])
})

test('after /clear the next turn reports the new session id', async ($, on) => {
  const { posts, clock, swapSession } = world(on, ENV)
  await $.session.start(START)
  await $.session.end({ reason: 'clear' } as any)
  swapSession('sid-2')
  await $.turn.start({ text: 'again', turnId: 't2' })
  await clock.settle()
  const ev = posts.filter(p => p.path === '/v1/event').map(p => p.body)
  expect(ev.map(b => [b.event, b.session_id])).toEqual([['SessionEnd', 'sid-1'], ['UserPromptSubmit', 'sid-2']])
})
