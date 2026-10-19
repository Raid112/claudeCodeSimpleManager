import type { EngineInterface } from 'claude-code'

// Loopback client for terminal/mod_bridge.py. Every send is fire-and-forget on one
// serial chain: hooks never wait on the manager, and the manager still receives
// events in the order they happened (`seq` is assigned at enqueue time).

const POST_TIMEOUT_MS = 2000

export type Link = { url: string; token: string }

let link: Link | null = null
let seq = 0
let chain: Promise<void> = Promise.resolve()

export function connect(next: Link | null): void {
  link = next
}

export function isConnected(): boolean {
  return link !== null
}

export function send($: EngineInterface, path: string, body: Record<string, unknown>): Promise<void> {
  const target = link
  if (!target) return chain
  const payload = JSON.stringify({ seq: ++seq, ...body })
  chain = chain.then(() => deliver($, target, path, payload))
  return chain
}

async function deliver($: EngineInterface, target: Link, path: string, payload: string): Promise<void> {
  // A hung manager must not stall the chain: give up after POST_TIMEOUT_MS. The
  // timer is its own dispatch, so the wait costs no hook any budget.
  let timer: { cancel: () => void } | undefined
  const timeout = new Promise<void>(resolve => {
    timer = $.clock.after(POST_TIMEOUT_MS, resolve)
  })
  const request = $.http
    .fetch(`${target.url}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-cm-tab-token': target.token },
      body: payload,
    })
    .then(
      () => undefined,
      () => undefined, // manager down or refused: fail open, the session goes on
    )
  await Promise.race([request, timeout])
  timer?.cancel()
}
