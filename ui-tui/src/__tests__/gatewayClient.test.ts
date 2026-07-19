import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

interface ListenerEntry {
  callback: (event: any) => void
  once: boolean
}

const { FakeAgent, FakeWebSocket } = vi.hoisted(() => {
  class FakeAgent {
    static instances: FakeAgent[] = []

    readonly options: unknown
    closed = false

    constructor(options: unknown) {
      this.options = options
      FakeAgent.instances.push(this)
    }

    close() {
      this.closed = true

      return Promise.resolve()
    }

    static reset() {
      FakeAgent.instances = []
    }
  }

  class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    static instances: FakeWebSocket[] = []

    readyState = FakeWebSocket.CONNECTING
    sent: string[] = []
    readonly url: string
    readonly options: unknown
    private listeners = new Map<string, ListenerEntry[]>()

    constructor(url: string, options?: unknown) {
      this.url = url
      this.options = options
      FakeWebSocket.instances.push(this)
    }

    static reset() {
      FakeWebSocket.instances = []
    }

    addEventListener(type: string, callback: (event: any) => void, options?: unknown) {
      const once =
        typeof options === 'object' &&
        options !== null &&
        'once' in options &&
        Boolean((options as { once?: unknown }).once)

      const entries = this.listeners.get(type) ?? []

      entries.push({ callback, once })
      this.listeners.set(type, entries)
    }

    removeEventListener(type: string, callback: (event: any) => void) {
      const entries = this.listeners.get(type)

      if (!entries) {
        return
      }

      this.listeners.set(
        type,
        entries.filter(entry => entry.callback !== callback)
      )
    }

    send(payload: string) {
      if (this.readyState !== FakeWebSocket.OPEN) {
        throw new Error('socket not open')
      }

      this.sent.push(payload)
    }

    close(code = 1000) {
      if (this.readyState === FakeWebSocket.CLOSED) {
        return
      }

      this.readyState = FakeWebSocket.CLOSED
      this.emit('close', { code })
    }

    open() {
      this.readyState = FakeWebSocket.OPEN
      this.emit('open', {})
    }

    message(data: string) {
      this.emit('message', { data })
    }

    private emit(type: string, event: any) {
      const entries = [...(this.listeners.get(type) ?? [])]

      for (const entry of entries) {
        entry.callback(event)

        if (entry.once) {
          this.removeEventListener(type, entry.callback)
        }
      }
    }
  }

  return { FakeAgent, FakeWebSocket }
})

vi.mock('undici', () => ({ Agent: FakeAgent, WebSocket: FakeWebSocket }))

import { GatewayClient } from '../gatewayClient.js'

describe('GatewayClient websocket attach mode', () => {
  const originalWebSocket = globalThis.WebSocket
  let originalGatewayUrl: string | undefined
  let originalSidecarUrl: string | undefined
  let originalOwnerWorkerAttach: string | undefined
  let originalGatewaySocketPath: string | undefined

  beforeEach(() => {
    originalGatewayUrl = process.env.HERMES_TUI_GATEWAY_URL
    originalSidecarUrl = process.env.HERMES_TUI_SIDECAR_URL
    originalOwnerWorkerAttach = process.env.HERMES_OWNER_WORKER_TUI_ATTACH
    originalGatewaySocketPath = process.env.HERMES_TUI_GATEWAY_SOCKET_PATH
    FakeAgent.reset()
    FakeWebSocket.reset()
    ;(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    if (originalGatewayUrl === undefined) {
      delete process.env.HERMES_TUI_GATEWAY_URL
    } else {
      process.env.HERMES_TUI_GATEWAY_URL = originalGatewayUrl
    }

    if (originalSidecarUrl === undefined) {
      delete process.env.HERMES_TUI_SIDECAR_URL
    } else {
      process.env.HERMES_TUI_SIDECAR_URL = originalSidecarUrl
    }

    if (originalOwnerWorkerAttach === undefined) {
      delete process.env.HERMES_OWNER_WORKER_TUI_ATTACH
    } else {
      process.env.HERMES_OWNER_WORKER_TUI_ATTACH = originalOwnerWorkerAttach
    }

    if (originalGatewaySocketPath === undefined) {
      delete process.env.HERMES_TUI_GATEWAY_SOCKET_PATH
    } else {
      process.env.HERMES_TUI_GATEWAY_SOCKET_PATH = originalGatewaySocketPath
    }

    FakeAgent.reset()
    FakeWebSocket.reset()

    if (originalWebSocket) {
      globalThis.WebSocket = originalWebSocket
    } else {
      delete (globalThis as { WebSocket?: unknown }).WebSocket
    }
  })

  it('fails closed instead of spawning a standalone gateway when owner attach is required', () => {
    process.env.HERMES_OWNER_WORKER_TUI_ATTACH = '1'
    delete process.env.HERMES_TUI_GATEWAY_URL
    const spawnGateway = vi.spyOn(GatewayClient.prototype as any, 'startSpawnedGateway')
    const gw = new GatewayClient()

    gw.start()

    expect(FakeWebSocket.instances).toHaveLength(0)
    expect(spawnGateway).not.toHaveBeenCalled()
    expect(gw.getLogTail(20)).toContain('[startup] owner-worker gateway attach URL unavailable')
  })

  it('connects owner-worker attach through the exact Unix socket dispatcher', () => {
    process.env.HERMES_OWNER_WORKER_TUI_ATTACH = '1'
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://owner-worker/api/ws?owner_tui_attach=one-use-token'
    process.env.HERMES_TUI_GATEWAY_SOCKET_PATH = '/run/hermes/worker.sock'
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingGlobalWebSocket extends FakeWebSocket {
      constructor(url: string) {
        throw new Error(`unexpected global websocket: ${url}`)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()

    expect(FakeAgent.instances).toHaveLength(1)
    expect(FakeAgent.instances[0]?.options).toEqual({ connect: { socketPath: '/run/hermes/worker.sock' } })
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0]?.url).toBe('ws://owner-worker/api/ws?owner_tui_attach=one-use-token')
    expect(FakeWebSocket.instances[0]?.options).toEqual({ dispatcher: FakeAgent.instances[0] })
    expect(gw.requiresFreshOwnerAttachOnExit()).toBe(true)

    gw.kill()
    expect(FakeAgent.instances[0]?.closed).toBe(true)
  })

  it.each([
    ['missing socket', 'ws://owner-worker/api/ws?owner_tui_attach=abc', undefined],
    ['relative socket', 'ws://owner-worker/api/ws?owner_tui_attach=abc', 'runtime/worker.sock'],
    ['foreign host', 'ws://gateway.test/api/ws?owner_tui_attach=abc', '/run/hermes/worker.sock'],
    ['wrong path', 'ws://owner-worker/other?owner_tui_attach=abc', '/run/hermes/worker.sock'],
    ['duplicate token', 'ws://owner-worker/api/ws?owner_tui_attach=abc&owner_tui_attach=def', '/run/hermes/worker.sock'],
    ['extra parameter', 'ws://owner-worker/api/ws?owner_tui_attach=abc&channel=demo', '/run/hermes/worker.sock']
  ])('fails closed for invalid owner attach: %s', (_label, url, socketPath) => {
    process.env.HERMES_OWNER_WORKER_TUI_ATTACH = '1'
    process.env.HERMES_TUI_GATEWAY_URL = url

    if (socketPath) {
      process.env.HERMES_TUI_GATEWAY_SOCKET_PATH = socketPath
    } else {
      delete process.env.HERMES_TUI_GATEWAY_SOCKET_PATH
    }

    const spawnGateway = vi.spyOn(GatewayClient.prototype as any, 'startSpawnedGateway')
    const gw = new GatewayClient()

    gw.start()

    expect(FakeAgent.instances).toHaveLength(0)
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect(spawnGateway).not.toHaveBeenCalled()
    expect(gw.getLogTail(20)).toContain('[startup] owner-worker gateway attach configuration invalid')
  })

  it('closes the owner dispatcher when the attached websocket closes', async () => {
    process.env.HERMES_OWNER_WORKER_TUI_ATTACH = '1'
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://owner-worker/api/ws?owner_tui_attach=abc'
    process.env.HERMES_TUI_GATEWAY_SOCKET_PATH = '/run/hermes/worker.sock'
    const gw = new GatewayClient()

    gw.start()
    const dispatcher = FakeAgent.instances[0]!
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()
    await Promise.resolve()
    gatewaySocket.close(1006)

    expect(dispatcher.closed).toBe(true)
  })

  it('waits for websocket open and resolves RPC requests', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!
    const req = gw.request<{ ok: boolean }>('session.create', { cols: 80 })

    expect(gatewaySocket.sent).toHaveLength(0)
    gatewaySocket.open()
    await vi.waitFor(() => expect(gatewaySocket.sent).toHaveLength(1))

    const frame = JSON.parse(gatewaySocket.sent[0] ?? '{}') as { id: string; method: string }
    expect(frame.method).toBe('session.create')

    gatewaySocket.message(JSON.stringify({ id: frame.id, jsonrpc: '2.0', result: { ok: true } }))
    await expect(req).resolves.toEqual({ ok: true })

    gw.kill()
  })

  it('drains buffered events on a later microtask, not synchronously inside drain()', async () => {
    // Regression for #36658: in attach mode the already-running gateway
    // replays `gateway.ready` the instant the socket connects, so it lands in
    // bufferedEvents BEFORE the consumer's mount-time subscribe effect runs.
    // If drain() emitted those synchronously, the gateway.ready handler's
    // setState cascade would run inside React's first commit -> "Too many
    // re-renders" (#301). drain() must defer the buffered flush so the first
    // commit settles first.
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    // Server replays ready BEFORE the consumer subscribes (attach-mode timing):
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
    )

    const order: string[] = []

    gw.on('event', ev => order.push(`event:${ev.type}`))
    gw.drain()
    order.push('after-drain')

    // Buffered event must NOT have fired synchronously inside drain():
    expect(order).toEqual(['after-drain'])

    // ...and must arrive on the next microtask.
    await vi.waitFor(() => expect(order).toContain('event:gateway.ready'))
    expect(order).toEqual(['after-drain', 'event:gateway.ready'])

    gw.kill()
  })

  it('preserves FIFO order when a live event arrives before the deferred flush', async () => {
    // #36658 hardening: `subscribed` must NOT flip synchronously in drain().
    // A live event delivered in the window between drain() returning and the
    // deferred microtask running must still queue BEHIND the chronologically
    // earlier buffered events, not jump ahead of them.
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    // Buffered first (replayed on connect, before subscribe):
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
    )

    const order: string[] = []

    gw.on('event', ev => order.push(ev.type))
    gw.drain()

    // A LIVE event arrives synchronously in the post-drain / pre-microtask gap:
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'session.info', payload: {} } })
    )

    // Nothing emitted yet (subscribed stays false until the microtask):
    expect(order).toEqual([])

    await vi.waitFor(() => expect(order.length).toBe(2))
    // FIFO preserved: the earlier-buffered gateway.ready precedes the live one.
    expect(order).toEqual(['gateway.ready', 'session.info'])

    gw.kill()
  })

  it('mirrors event frames to sidecar websocket when configured', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = 'ws://gateway.test/api/pub?token=abc&channel=demo'

    const gw = new GatewayClient()
    const seen: string[] = []

    gw.on('event', ev => seen.push(ev.type))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const sidecarSocket = FakeWebSocket.instances[1]!

    sidecarSocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle so
    // the subsequent live event takes the synchronous publish path.
    await Promise.resolve()

    const eventFrame = JSON.stringify({
      jsonrpc: '2.0',
      method: 'event',
      params: { type: 'tool.start', payload: { tool_id: 't1' } }
    })

    gatewaySocket.message(eventFrame)

    expect(seen).toContain('tool.start')
    expect(sidecarSocket.sent).toContain(eventFrame)

    gw.kill()
  })

  it('publishes local dashboard-control events to the sidecar websocket', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = 'ws://gateway.test/api/pub?token=abc&channel=demo'

    const gw = new GatewayClient()
    const seen: string[] = []

    gw.on('event', ev => seen.push(ev.type))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const sidecarSocket = FakeWebSocket.instances[1]!

    sidecarSocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle.
    await Promise.resolve()

    gw.publishLocalEvent({
      payload: { reason: 'idle_exit_hotkey' },
      session_id: 'sid-old',
      type: 'dashboard.new_session_requested'
    })

    expect(seen).toContain('dashboard.new_session_requested')
    expect(JSON.parse(sidecarSocket.sent.at(-1) ?? '{}')).toEqual({
      jsonrpc: '2.0',
      method: 'event',
      params: {
        payload: { reason: 'idle_exit_hotkey' },
        session_id: 'sid-old',
        type: 'dashboard.new_session_requested'
      }
    })

    gw.kill()
  })

  it('emits exit when attached websocket closes', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    const exits: Array<null | number> = []

    gw.on('exit', code => exits.push(code))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle so
    // the close below takes the synchronous exit path.
    await Promise.resolve()
    gatewaySocket.close(1011)

    expect(exits).toEqual([1011])
    expect(gw.getLogTail(20)).toContain('[lifecycle] websocket close code=1011')
    expect(gw.getLogTail(20)).toContain('[lifecycle] transport exit code=1011')
  })

  it('rejects pending RPCs with websocket wording when the attached socket closes', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()

    const req = gw.request('session.create', {})
    await vi.waitFor(() => expect(gatewaySocket.sent.length).toBeGreaterThan(0))

    gatewaySocket.close(1011)

    await expect(req).rejects.toThrow(/gateway websocket closed \(1011\)/)
  })

  it('rejects pending RPCs when kill() closes the attached websocket', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()

    const req = gw.request('session.create', {})
    await vi.waitFor(() => expect(gatewaySocket.sent.length).toBeGreaterThan(0))

    gw.kill('test.shutdown')

    await expect(req).rejects.toThrow(/gateway closed/)
    expect(gw.getLogTail(20)).toContain('[lifecycle] GatewayClient.kill reason=test.shutdown')
  })

  it('reattaches when HERMES_TUI_GATEWAY_URL rotates between requests', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway-old.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const firstSocket = FakeWebSocket.instances[0]!

    firstSocket.open()
    gw.drain()

    const stale = gw.request('session.create', {})
    await vi.waitFor(() => expect(firstSocket.sent.length).toBeGreaterThan(0))

    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway-new.test/api/ws?token=xyz'
    const next = gw.request('session.create', {})

    await expect(stale).rejects.toThrow(/gateway attach url changed/)
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const secondSocket = FakeWebSocket.instances[1]!
    expect(secondSocket.url).toContain('gateway-new.test')

    secondSocket.open()
    await vi.waitFor(() => expect(secondSocket.sent.length).toBeGreaterThan(0))

    const frame = JSON.parse(secondSocket.sent[0] ?? '{}') as { id: string }
    secondSocket.message(JSON.stringify({ id: frame.id, jsonrpc: '2.0', result: { ok: true } }))

    await expect(next).resolves.toEqual({ ok: true })
    gw.kill()
  })

  it('uses the undici WebSocket fallback when global WebSocket is unavailable', () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=hunter2&channel=secret'
    delete (globalThis as { WebSocket?: unknown }).WebSocket

    const gw = new GatewayClient()

    gw.start()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0]?.url).toBe('ws://gateway.test/api/ws?token=hunter2&channel=secret')

    gw.kill()
  })

  it('redacts attach URL secrets when the WebSocket constructor throws', () => {
    const secretUrl = 'ws://gateway.test/api/ws?token=hunter2&channel=secret'

    process.env.HERMES_TUI_GATEWAY_URL = secretUrl
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        throw new TypeError(`Invalid URL: ${url}`)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    gw.drain()

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('channel=secret')
    expect(tail).not.toContain(secretUrl)
    expect(tail).toContain('ws://gateway.test/api/ws?***')

    gw.kill()
  })

  it('redacts sidecar URL secrets when the WebSocket constructor throws', async () => {
    const sidecarUrl = 'ws://gateway.test/api/pub?token=hunter2&channel=secret'

    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = sidecarUrl
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingSidecarWebSocket extends FakeWebSocket {
      constructor(url: string) {
        if (url.includes('/api/pub')) {
          throw new TypeError(`Invalid URL: ${url}`)
        }

        super(url)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    await vi.waitFor(() => expect(gw.getLogTail(20)).toContain('[sidecar] failed to connect'))

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('channel=secret')
    expect(tail).not.toContain(sidecarUrl)
    expect(tail).toContain('ws://gateway.test/api/pub?***')

    gw.kill()
  })

  it('redacts user-info credentials even on URLs the WHATWG parser rejects', () => {
    // Port 99999 is outside the WHATWG URL parser's valid 0–65535
    // range and survives `.trim()`, so the fixture deterministically
    // exercises `redactUrl()`'s fallback branch across Node versions.
    // (An earlier `%zz` user-info fixture did NOT actually throw in
    // recent Node — WHATWG accepts malformed percent escapes there —
    // which silently routed the test through the structured-URL path.)
    const fixture = 'ws://alice:hunter2@gateway.test:99999/api/ws?token=secret'
    expect(() => new URL(fixture)).toThrow()

    process.env.HERMES_TUI_GATEWAY_URL = fixture
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        throw new TypeError(`Invalid URL: ${url}`)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    gw.drain()

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('alice')
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('token=secret')

    gw.kill()
  })
})
