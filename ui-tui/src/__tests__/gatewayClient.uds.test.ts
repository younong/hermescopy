import { createHash } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import type { Socket } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { GatewayClient } from '../gatewayClient.js'

const websocketFrame = (text: string) => {
  const payload = Buffer.from(text)

  if (payload.length >= 126) {
    throw new Error('test frame is too large')
  }

  return Buffer.concat([Buffer.from([0x81, payload.length]), payload])
}

const decodeMaskedFrame = (frame: Buffer) => {
  const masked = Boolean(frame[1]! & 0x80)
  const length = frame[1]! & 0x7f

  if (!masked || length >= 126) {
    throw new Error('unexpected client frame')
  }

  const mask = frame.subarray(2, 6)
  const payload = frame.subarray(6, 6 + length)
  const decoded = Buffer.alloc(length)

  for (let index = 0; index < length; index += 1) {
    decoded[index] = payload[index]! ^ mask[index % 4]!
  }

  return decoded.toString('utf8')
}

describe.skipIf(process.platform === 'win32')('GatewayClient owner-worker Unix socket transport', () => {
  const originalWebSocket = globalThis.WebSocket
  const originalGatewayUrl = process.env.HERMES_TUI_GATEWAY_URL
  const originalOwnerWorkerAttach = process.env.HERMES_OWNER_WORKER_TUI_ATTACH
  const originalGatewaySocketPath = process.env.HERMES_TUI_GATEWAY_SOCKET_PATH

  afterEach(() => {
    if (originalWebSocket) {
      globalThis.WebSocket = originalWebSocket
    } else {
      delete (globalThis as { WebSocket?: unknown }).WebSocket
    }

    for (const [key, value] of [
      ['HERMES_TUI_GATEWAY_URL', originalGatewayUrl],
      ['HERMES_OWNER_WORKER_TUI_ATTACH', originalOwnerWorkerAttach],
      ['HERMES_TUI_GATEWAY_SOCKET_PATH', originalGatewaySocketPath]
    ] as const) {
      if (value === undefined) {
        delete process.env[key]
      } else {
        process.env[key] = value
      }
    }
  })

  it('performs a real JSON-RPC exchange through AF_UNIX', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hws-'))
    const socketPath = join(root, 'w.sock')
    const server = createServer()
    let acceptedSocket: Socket | null = null
    let requestUrl = ''
    let requestHost = ''

    server.on('upgrade', (request, socket) => {
      acceptedSocket = socket
      requestUrl = request.url ?? ''
      requestHost = request.headers.host ?? ''
      const key = request.headers['sec-websocket-key']

      const accept = createHash('sha1')
        .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest('base64')

      socket.write(
        `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`
      )
      socket.once('data', frame => {
        const requestFrame = JSON.parse(decodeMaskedFrame(frame)) as { id: string; method: string }

        socket.write(
          websocketFrame(JSON.stringify({ id: requestFrame.id, jsonrpc: '2.0', result: { method: requestFrame.method } }))
        )
      })
    })

    await new Promise<void>((resolve, reject) => {
      server.once('error', reject)
      server.listen(socketPath, resolve)
    })

    process.env.HERMES_OWNER_WORKER_TUI_ATTACH = '1'
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://owner-worker/api/ws?owner_tui_attach=one-use-token'
    process.env.HERMES_TUI_GATEWAY_SOCKET_PATH = socketPath
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingWebSocket {
      constructor() {
        throw new Error('owner attach must not use the global WebSocket')
      }
    }

    const gw = new GatewayClient()

    try {
      gw.start()
      await expect(gw.request('session.create', {})).resolves.toEqual({ method: 'session.create' })
      expect(requestUrl).toBe('/api/ws?owner_tui_attach=one-use-token')
      expect(requestHost).toBe('owner-worker')
    } finally {
      gw.kill('test.shutdown')
      acceptedSocket?.destroy()
      await new Promise<void>(resolve => server.close(() => resolve()))
      await rm(root, { force: true, recursive: true })
    }
  })
})
