import { describe, expect, test } from 'vitest'
import { createConsoleRuntime } from './runtime'

const token = 'relay-token-that-is-at-least-32-characters-long'

describe('console runtime media source', () => {
  test('derives the relay media bootstrap source from the WebSocket origin and bearer', () => {
    const runtime = createConsoleRuntime({
      baseUrl: 'ws://10.10.1.60:8000/ws',
      sessionId: 'sweep-4',
      token,
    })

    expect(runtime.mediaConfigurationSource).toEqual({
      url: 'http://10.10.1.60:8000/runtime-config.json',
      authorization: `Bearer ${token}`,
    })
  })

  test('uses HTTPS for a secure relay', () => {
    const runtime = createConsoleRuntime({
      baseUrl: 'wss://relay.example.internal',
      sessionId: 'sweep-4',
      token,
    })

    expect(runtime.mediaConfigurationSource?.url).toBe(
      'https://relay.example.internal/runtime-config.json',
    )
  })

  test('has no relay media source without a relay bootstrap', () => {
    expect(createConsoleRuntime(undefined).mediaConfigurationSource).toBeNull()
    expect(
      createConsoleRuntime({ baseUrl: '', sessionId: 'demo', token }).mediaConfigurationSource,
    ).toBeNull()
  })
})
