import { describe, expect, test } from 'vitest'
import { WebSocketRelayClient, buildSessionWebSocketUrl } from './client'
import { publicNodeEvents } from '../testing/public-node-events'

class TestSocket extends EventTarget {
  readyState = 1
  sent: string[] = []

  send(payload: string) {
    this.sent.push(payload)
  }

  close() {}

  open() {
    this.dispatchEvent(new Event('open'))
  }

  message(payload: object) {
    this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(payload) }))
  }

  closed(code = 1000) {
    this.dispatchEvent(new CloseEvent('close', { code }))
  }

  failed() {
    this.dispatchEvent(new Event('error'))
  }
}

describe('WebSocket relay client', () => {
  test.each(['console', 'keyboard', 'webcam', 'language'] as const)('keeps %s connected through public phone events and a higher-epoch rejoin', (source) => {
    const socket = new TestSocket()
    const statuses: string[] = []
    const serverTypes: string[] = []
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source, token: 'test-token' },
      { now: () => 100, createSocket: () => socket as unknown as WebSocket },
    )
    client.subscribe((event) => {
      if (event.kind === 'connection') statuses.push(event.connection.status)
      else serverTypes.push(event.event.type)
    })
    client.start()
    socket.open()
    socket.message({ v: 1, t: 100, type: 'auth.accepted', event_id: 'auth-node-events', session: 'session-1', source, drone_id: null })
    const joined = { v: 1, t: 101, type: 'membership', event_id: 'join-5', session: 'session-1',
      roster_version: 12, action: 'join', drone_id: 2, connection_epoch: 5, membership: 'registered',
      readiness_reasons: ['telemetry_missing'], adapter_id: 'test-android-node', capabilities: ['flight', 'body_pulse_v1'],
      provenance: 'adapter_signature', reason: null }
    socket.message(joined)
    publicNodeEvents('session-1', 5).forEach((event) => socket.message(event))
    socket.message({ ...joined, event_id: 'lost-5', roster_version: 13, action: 'unexpected_loss', membership: 'disconnected', provenance: 'relay_transport_attestation', reason: 'socket_closed' })
    socket.message({ ...joined, event_id: 'join-6', roster_version: 14, connection_epoch: 6 })
    publicNodeEvents('session-1', 6).forEach((event) => socket.message(event))
    expect(statuses.at(-1)).toBe('connected')
    expect(statuses).not.toContain('degraded')
    expect(serverTypes.filter((type) => type === 'capabilities')).toHaveLength(2)
    expect(serverTypes.filter((type) => type === 'node_status')).toHaveLength(2)
    const count = serverTypes.length
    socket.message({ ...publicNodeEvents('session-1')[1], watchdog_state: 'invented' })
    expect(statuses.at(-1)).toBe('degraded')
    expect(serverTypes).toHaveLength(count)
    socket.message({ ...joined, type: 'unknown_frame' })
    expect(serverTypes).toHaveLength(count)
  })

  test('puts no token in the URL and sends the strict first auth frame', () => {
    const socket = new TestSocket()
    const client = new WebSocketRelayClient(
      {
        baseUrl: 'wss://relay.example.test/base?token=leak',
        sessionId: 'incident / alpha',
        source: 'console',
        token: 'secret-token',
      },
      {
        now: () => 100,
        createSocket: () => socket as unknown as WebSocket,
      },
    )

    client.start()
    socket.open()

    expect(socket.sent).toHaveLength(1)
    expect(JSON.parse(socket.sent[0])).toEqual({
      v: 1,
      type: 'auth',
      source: 'console',
      token: 'secret-token',
    })
    expect(buildSessionWebSocketUrl({ baseUrl: 'wss://relay.test', sessionId: 's 1' })).toBe(
      'wss://relay.test/ws/s%201',
    )
  })

  test('does not authenticate on socket open or pre-auth state', async () => {
    const socket = new TestSocket()
    const statuses: string[] = []
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
      { now: () => 100, createSocket: () => socket as unknown as WebSocket },
    )
    client.subscribe((event) => {
      if (event.kind === 'connection') statuses.push(event.connection.status)
    })
    client.start()
    socket.open()

    await expect(
      client.sendIntent({
        v: 1,
        t: 100,
        type: 'intent',
        intent_id: 'intent-1',
        retry_of: null,
        source: 'console',
        session: 'session-1',
        name: 'hold',
        args: {},
        selection: [1],
        mode: 'indoor',
        confirm: false,
      }),
    ).rejects.toThrow('not authenticated')
    expect(statuses.at(-1)).toBe('connecting')

    socket.message({
      v: 1,
      t: 101,
      type: 'auth.accepted',
      event_id: 'auth-1',
      session: 'session-1',
      source: 'console',
      drone_id: null,
    })
    expect(statuses.at(-1)).toBe('connected')
  })

  test('ignores late events from a socket replaced during StrictMode cleanup', () => {
    const first = new TestSocket()
    const second = new TestSocket()
    const sockets = [first, second]
    const statuses: string[] = []
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
      { now: () => 100, createSocket: () => sockets.shift() as unknown as WebSocket },
    )
    client.subscribe((event) => {
      if (event.kind === 'connection') statuses.push(event.connection.status)
    })

    client.start()
    client.stop()
    client.start()
    second.open()
    second.message({
      v: 1,
      t: 101,
      type: 'auth.accepted',
      event_id: 'auth-new',
      session: 'session-1',
      source: 'console',
      drone_id: null,
    })
    first.message({ stale: true })
    first.failed()
    first.closed(1006)

    expect(statuses.at(-1)).toBe('connected')
  })

  test('accepts telemetry before authoritative state without degrading the connection', () => {
    const socket = new TestSocket()
    const statuses: string[] = []
    const serverTypes: string[] = []
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
      { now: () => 100, createSocket: () => socket as unknown as WebSocket },
    )
    client.subscribe((event) => {
      if (event.kind === 'connection') statuses.push(event.connection.status)
      else serverTypes.push(event.event.type)
    })
    client.start()
    socket.open()
    socket.message({
      v: 1,
      t: 101,
      type: 'auth.accepted',
      event_id: 'auth-telemetry-test',
      session: 'session-1',
      source: 'console',
      drone_id: null,
    })
    socket.message({
      v: 1,
      t: 102,
      type: 'telemetry',
      event_id: 'telemetry-1',
      session: 'session-1',
      drone: 1,
      connection_epoch: 2,
      x: 1,
      y: 2,
      z: 0.5,
      vx: 0,
      vy: 0,
      vz: 0,
      battery: 0.8,
      state: 'hovering',
      link: 0.9,
      pos_quality: 0.95,
    })

    expect(statuses.at(-1)).toBe('connected')
    expect(serverTypes).toEqual(['auth.accepted', 'telemetry'])
  })
})
