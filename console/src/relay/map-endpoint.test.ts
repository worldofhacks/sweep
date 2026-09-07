import { describe, expect, test } from 'vitest'
import { relayMapEndpoint } from './map-endpoint'
import { relayHttpOrigin, relayHttpUrl } from './origin'

describe('relay map endpoint', () => {
  test('the map lives under the relay base read as HTTP, like the transcripts endpoint', () => {
    expect(relayMapEndpoint('wss://relay.test', 'sweep-6', 'token')).toEqual({
      url: 'https://relay.test/api/sessions/sweep-6/map',
      resetUrl: 'https://relay.test/api/sessions/sweep-6/map/reset',
      authorization: 'Bearer token',
    })
    expect(relayMapEndpoint('ws://127.0.0.1:8000', 'sweep-6', 'token')?.url).toBe(
      'http://127.0.0.1:8000/api/sessions/sweep-6/map',
    )
    expect(relayMapEndpoint('wss://relay.test/internal/', 'sweep-6', 'token')?.url).toBe(
      'https://relay.test/internal/api/sessions/sweep-6/map',
    )
  })

  test('a session id that is not a path segment is escaped, never joined raw', () => {
    expect(relayMapEndpoint('wss://relay.test', 'a/b', 'token')?.url).toBe(
      'https://relay.test/api/sessions/a%2Fb/map',
    )
  })

  test('without an origin, a session, or a token there is no endpoint', () => {
    expect(relayMapEndpoint('not a url', 'sweep-6', 'token')).toBeNull()
    expect(relayMapEndpoint('ftp://relay.test', 'sweep-6', 'token')).toBeNull()
    expect(relayMapEndpoint('wss://relay.test', '', 'token')).toBeNull()
    expect(relayMapEndpoint('wss://relay.test', 'sweep-6', '')).toBeNull()
  })

  test('the origin form keeps host and port and drops the path, for root endpoints', () => {
    expect(relayHttpOrigin('wss://relay.test:8443/internal?x=1')).toBe('https://relay.test:8443')
    expect(relayHttpOrigin('https://relay.test')).toBe('https://relay.test')
    expect(relayHttpOrigin('')).toBeNull()
  })

  test('credentials, query and fragment never survive into an endpoint', () => {
    expect(relayHttpUrl('wss://user:secret@relay.test/base?x=1#y', '/api/ping')).toBe(
      'https://relay.test/base/api/ping',
    )
    expect(relayHttpUrl('ftp://relay.test', '/api/ping')).toBeNull()
  })
})
