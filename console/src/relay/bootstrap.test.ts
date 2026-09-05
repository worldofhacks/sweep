import { afterEach, describe, expect, test, vi } from 'vitest'
import { bootstrapConsoleRuntime, loadRelayBootstrap, normalizeRelayBootstrap } from './bootstrap'
import {
  DEFAULT_RELAY_ORIGIN,
  DEFAULT_RELAY_SESSION_ID,
  RELAY_BOOTSTRAP_ENDPOINT,
  relayFromEnvironment,
} from './bootstrap-endpoint'
import type { RelayConnection } from '../control/state'

const bootstrap = {
  baseUrl: 'ws://127.0.0.1:8000',
  sessionId: 'demo',
  token: 'relay-token-that-must-not-be-echoed',
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('relay bootstrap endpoint payload', () => {
  test('builds the payload from the relay variable names with loopback defaults', () => {
    expect(relayFromEnvironment({ SWEEP_RELAY_TOKEN: 'token' })).toEqual({
      baseUrl: DEFAULT_RELAY_ORIGIN,
      sessionId: DEFAULT_RELAY_SESSION_ID,
      token: 'token',
    })
    expect(
      relayFromEnvironment({
        SWEEP_RELAY_ORIGIN: 'wss://relay.example.internal',
        SWEEP_SESSION_ID: 'incident-7',
        SWEEP_RELAY_TOKEN: 'token',
      }),
    ).toEqual({ baseUrl: 'wss://relay.example.internal', sessionId: 'incident-7', token: 'token' })
  })

  test.each([
    ['unset', {}],
    ['empty', { SWEEP_RELAY_TOKEN: '' }],
  ])('has no payload while the token is %s, whatever else is set', (_name, env) => {
    expect(
      relayFromEnvironment({ SWEEP_RELAY_ORIGIN: 'ws://127.0.0.1:8000', SWEEP_SESSION_ID: 'demo', ...env }),
    ).toBeNull()
  })
})

describe('relay bootstrap loader', () => {
  test('reads the bootstrap once from the same-origin endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({ relay: bootstrap }))

    await expect(loadRelayBootstrap(fetcher)).resolves.toEqual(bootstrap)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledWith(RELAY_BOOTSTRAP_ENDPOINT, {
      cache: 'no-store',
      credentials: 'same-origin',
    })
  })

  test('leaves the console disconnected when the endpoint answers 503', async () => {
    const report = vi.fn()
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({ relay: null }, 503))

    await expect(loadRelayBootstrap(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledWith('Relay bootstrap unavailable: network controls stay disconnected.')
  })

  test.each([
    ['a missing token', { baseUrl: bootstrap.baseUrl, sessionId: bootstrap.sessionId }],
    ['an empty session', { ...bootstrap, sessionId: '' }],
    ['an empty relay URL', { ...bootstrap, baseUrl: '' }],
    ['an HTTP relay URL', { ...bootstrap, baseUrl: 'http://127.0.0.1:8000' }],
    ['an unparseable relay URL', { ...bootstrap, baseUrl: 'not a URL' }],
    ['a non-string token', { ...bootstrap, token: 42 }],
    ['a null relay', null],
    ['a list', [bootstrap]],
  ])('refuses a payload with %s without echoing it', async (_name, relay) => {
    const report = vi.fn()
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({ relay }))

    await expect(loadRelayBootstrap(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
    expect(report.mock.calls[0][0]).not.toContain(bootstrap.token)
  })

  test.each([
    ['network rejection', vi.fn().mockRejectedValue(new TypeError('offline'))],
    ['malformed JSON', vi.fn().mockResolvedValue(new Response('{', { status: 200 }))],
    ['a non-object body', vi.fn().mockResolvedValue(jsonResponse('relay'))],
  ])('degrades %s without blocking the console', async (_name, fetcher) => {
    const report = vi.fn()

    await expect(loadRelayBootstrap(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
  })

  test('normalizes only a complete ws or wss bootstrap and leaves the URL as given', () => {
    const secure = { ...bootstrap, baseUrl: 'wss://relay.example.internal/base' }

    expect(normalizeRelayBootstrap(secure)).toEqual(secure)
    expect(normalizeRelayBootstrap({ ...bootstrap, baseUrl: 'ftp://relay.example.internal' })).toBeUndefined()
    expect(normalizeRelayBootstrap(undefined)).toBeUndefined()
  })
})

describe('console runtime bootstrap', () => {
  afterEach(() => {
    delete window.__SWEEP_RELAY_CONFIG__
  })

  test('prefers the host global and never reads the endpoint', async () => {
    window.__SWEEP_RELAY_CONFIG__ = { ...bootstrap, sessionId: 'host-session' }
    const loader = vi.fn()

    const runtime = await bootstrapConsoleRuntime(loader)

    expect(loader).not.toHaveBeenCalled()
    expect(runtime.sessionId).toBe('host-session')
    expect(runtime.client.transport).toBe('websocket')
  })

  test('reads the endpoint once and only then creates the WebSocket runtime', async () => {
    const loader = vi.fn().mockResolvedValue(bootstrap)

    const runtime = await bootstrapConsoleRuntime(loader)

    expect(loader).toHaveBeenCalledOnce()
    expect(runtime.sessionId).toBe('demo')
    expect(runtime.client.transport).toBe('websocket')
    expect(runtime.keyboardClient.transport).toBe('websocket')
    expect(runtime.webcamClient.transport).toBe('websocket')
    expect(runtime.transcriptClient).not.toBeNull()
  })

  test('mounts exactly as before when neither source exists: disconnected and no retry', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    const connections: RelayConnection[] = []

    const runtime = await bootstrapConsoleRuntime(loader)
    runtime.client.subscribe((event) => {
      if (event.kind === 'connection') connections.push(event.connection)
    })
    runtime.client.start()

    expect(loader).toHaveBeenCalledOnce()
    expect(runtime.sessionId).toBe('session-unconfigured')
    expect(runtime.transcriptClient).toBeNull()
    expect(connections).toEqual([
      expect.objectContaining({
        status: 'disconnected',
        transport: 'unavailable',
        reason: expect.stringContaining('Relay bootstrap is not configured'),
      }),
    ])
    await expect(
      runtime.keyboardClient.sendIntent({} as Parameters<typeof runtime.keyboardClient.sendIntent>[0]),
    ).rejects.toThrow('Relay bootstrap is not configured')
  })
})
