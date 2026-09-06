import { describe, expect, test, vi } from 'vitest'
import { WebSocketRelayClient, buildSessionWebSocketUrl } from './client'

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


test('only a visible authenticated console refreshes presence and stopping clears its timer', () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const socket = new TestSocket()
  const client = new WebSocketRelayClient(
    { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
    { now: () => 100, createSocket: () => socket as unknown as WebSocket },
  )
  try {
    client.start()
    socket.open()
    vi.advanceTimersByTime(1_000)
    expect(socket.sent).toHaveLength(1)
    socket.message({
      v: 1, t: 101, type: 'auth.accepted', event_id: 'presence-auth',
      session: 'session-1', source: 'console', drone_id: null,
    })
    vi.advanceTimersByTime(1_000)
    expect(JSON.parse(socket.sent.at(-1)!)).toEqual({ v: 1, type: 'operator_presence' })
    const sent = socket.sent.length
    visibility.mockReturnValue('hidden')
    vi.advanceTimersByTime(2_000)
    expect(socket.sent).toHaveLength(sent)
    visibility.mockReturnValue('visible')
    client.stop()
    vi.advanceTimersByTime(2_000)
    expect(socket.sent).toHaveLength(sent)
  } finally {
    client.stop()
    visibility.mockRestore()
    vi.useRealTimers()
  }
})
