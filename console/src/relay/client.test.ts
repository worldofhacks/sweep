import { describe, expect, test } from 'vitest'
import { WebSocketRelayClient, buildSessionWebSocketUrl } from './client'
import type { SurveyLifecycleRequest } from './contract'

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
  test('sends survey completion only on its authenticated console session', async () => {
    const socket = new TestSocket()
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
      { now: () => 100, createSocket: () => socket as unknown as WebSocket },
    )
    const request: SurveyLifecycleRequest = {
      v: 1, t: 100, type: 'survey_lifecycle', event_id: 'finish-1', session: 'session-1',
      operation: 'complete', intent_id: 'survey-1', run_id: 'survey-survey-1', connection_epoch: 1,
    }
    await expect(client.sendSurveyLifecycle(request)).rejects.toThrow('not authenticated')
    client.start()
    socket.open()
    socket.message({ v: 1, t: 100, type: 'auth.accepted', event_id: 'auth-1', session: 'session-1', source: 'console', drone_id: null })
    await expect(client.sendSurveyLifecycle({ ...request, session: 'other' })).rejects.toThrow('session')
    await expect(client.sendSurveyLifecycle({ ...request, run_id: '' })).rejects.toThrow('session')
    await client.sendSurveyLifecycle(request)
    await client.sendSurveyLifecycle({ ...request, event_id: 'cancel-1', operation: 'cancel' })
    expect(socket.sent.slice(1).map((value) => JSON.parse(value))).toEqual([
      request, { ...request, event_id: 'cancel-1', operation: 'cancel' },
    ])
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

function observationFrame() {
  return {
    v: 1,
    type: 'observation',
    event_id: 'observation-socket-1',
    session: 'session-1',
    device_id: 1,
    connection_epoch: 2,
    source_id: 'ohmni-lidar',
    node_type: 'ground',
    frame: 'odom',
    confidence: 0.9,
    t_capture: null,
    t_source_receipt: { clock_id: 'ohmni', unit: 'ms', value: 101 },
    clock_mapping_id: null,
    payload: {
      kind: 'telemetry',
      position: { frame: 'odom', x_m: 1, y_m: 2, z_m: 0 },
      velocity: { frame: 'odom', x_m_s: 0, y_m_s: 0, z_m_s: 0 },
      battery: 0.8,
      link: 0.9,
      pos_quality: 0.7,
      state: 'ready',
    },
    t_ingest: 102,
  }
}

describe('observation socket events', () => {
  test('forwards a validated observation payload after console authentication', () => {
    const socket = new TestSocket()
    const events: string[] = []
    const client = new WebSocketRelayClient(
      { baseUrl: 'ws://localhost:8000', sessionId: 'session-1', source: 'console', token: 'token' },
      { now: () => 100, createSocket: () => socket as unknown as WebSocket },
    )
    client.subscribe((event) => {
      if (event.kind === 'server_event') events.push(event.event.type)
    })
    client.start()
    socket.open()
    socket.message({
      v: 1, t: 101, type: 'auth.accepted', event_id: 'auth-observation', session: 'session-1',
      source: 'console', drone_id: null,
    })
    socket.message(observationFrame())

    expect(events).toEqual(['auth.accepted', 'observation'])
  })
})
