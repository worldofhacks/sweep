import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AtlasClient } from '../atlas/client'
import { NativeAtlasClient } from '../atlas/native'
import { AnalysisRecoveryStore } from './analysisRecovery'

const connection = { baseUrl: 'https://relay.example', sessionId: 'room', token: 'private-workspace-key' }
const request = () => ({ revision: 2, ai: true, weather: false, audio_asset_id: null, request_id: crypto.randomUUID() })
let values: Map<string, string>
beforeEach(() => {
  values = new Map()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  })
  let queue: Promise<unknown> = Promise.resolve()
  vi.stubGlobal('navigator', { ...navigator, locks: { request: (_key: string, action: () => unknown) => {
    const result = queue.then(action)
    queue = result.catch(() => {})
    return result
  } } })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('survives a fresh client without saving media, scope identifiers, or credentials in plaintext', async () => {
  const proposed = request()
  await new AtlasClient(connection).analysisRecovery.reserve('private-space', 'private-capture', proposed)
  expect(await new AtlasClient(connection).analysisRecovery.read('private-space', 'private-capture')).toEqual(proposed)
  const disk = JSON.stringify([...values])
  for (const privateValue of [connection.token, connection.baseUrl, connection.sessionId, 'private-space', 'private-capture'])
    expect(disk).not.toContain(privateValue)
  const bucket = JSON.parse([...values.values()][0])
  expect(Object.values(bucket)).toEqual([proposed])
  expect(Object.keys(bucket)[0]).toMatch(/^[0-9a-f]{64}$/)
})

it('isolates exact workspace credentials, destinations, Spaces, and captures', async () => {
  const store = new AtlasClient(connection).analysisRecovery
  await store.reserve('place', 'capture', request())
  for (const change of [{ token: 'another-key' }, { baseUrl: 'https://other.example' }, { sessionId: 'other' }])
    expect(await new AtlasClient({ ...connection, ...change }).analysisRecovery.read('place', 'capture')).toBeNull()
  expect(await store.read('other', 'capture')).toBeNull()
  expect(await store.read('place', 'other')).toBeNull()
})

it('isolates native invitations even though their JavaScript bearer fields are empty', async () => {
  const session = { id: 'native-credential-one', baseUrl: connection.baseUrl, sessionId: connection.sessionId, space: 'place' }
  const original = new NativeAtlasClient(session)
  const proposed = request()
  await original.analysisRecovery.reserve('place', 'capture', proposed)
  expect(await new NativeAtlasClient(session).analysisRecovery.read('place', 'capture')).toEqual(proposed)
  expect(await new NativeAtlasClient({ ...session, id: 'native-credential-two' }).analysisRecovery.read('place', 'capture')).toBeNull()
  expect(await new AtlasClient({ ...connection, token: '' }).analysisRecovery.read('place', 'capture')).toBeNull()
  expect(JSON.stringify([...values])).not.toContain(session.id)
})

it('serializes competing reservations without replacing an earlier intent or its settings', async () => {
  const one = new AtlasClient(connection).analysisRecovery, two = new AtlasClient(connection).analysisRecovery
  const a = request(), b = { ...request(), weather: true }
  const outcomes = await Promise.all([one.reserve('place', 'capture', a), two.reserve('place', 'capture', b)])
  expect(outcomes.filter(result => !result.existing)).toHaveLength(1)
  expect(outcomes[0].request).toEqual(outcomes[1].request)
  expect(await one.read('place', 'capture')).toEqual(outcomes[0].request)
})

it('removes only the acknowledged identity and never erases a newer pending request', async () => {
  const store = new AtlasClient(connection).analysisRecovery
  const first = request(), next = request()
  await store.reserve('place', 'capture', first)
  expect(await store.acknowledge('place', 'capture', first.request_id)).toBeNull()
  expect(values.size).toBe(0)
  await store.reserve('place', 'capture', next)
  expect(await store.acknowledge('place', 'capture', first.request_id)).toEqual(next)
  expect(await store.read('place', 'capture')).toEqual(next)
})

it('bounds recovery storage without evicting unfinished intents', async () => {
  const store = new AtlasClient(connection).analysisRecovery
  const first = request()
  await store.reserve('place', '0', first)
  for (let i = 1; i < 20; i++) await store.reserve('place', String(i), request())
  await expect(store.reserve('place', '20', request())).rejects.toThrow('20 recovery references')
  expect(await store.read('place', '0')).toEqual(first)
  expect((await store.reserve('place', '0', request())).request).toEqual(first)
  await store.acknowledge('place', '0', first.request_id)
  expect((await store.reserve('place', '20', request())).existing).toBe(false)
})

it.each([{ name: 'broken JSON', raw: '{' }, { name: 'array', raw: '[]' }, { name: 'oversized', raw: 'x'.repeat(17000) }])('preserves an unreadable $name bucket rather than overwriting it', async ({ raw }) => {
  const store = new AtlasClient(connection).analysisRecovery
  await store.reserve('place', 'capture', request())
  const key = [...values.keys()][0]
  values.set(key, raw)
  await expect(store.read('place', 'capture')).rejects.toThrow('not been overwritten')
  await expect(store.reserve('place', 'other', request())).rejects.toThrow('not been overwritten')
  expect(values.get(key)).toBe(raw)
})

it('refuses unknown fields and storage or lock failures before admitting an intent', async () => {
  const store = new AnalysisRecoveryStore(['test'])
  await expect(store.reserve('place', 'capture', { ...request(), notes: 'Do not persist my story' } as ReturnType<typeof request>)).rejects.toThrow('not been overwritten')
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('Quota exceeded') })
  await expect(store.reserve('place', 'capture', request())).rejects.toThrow('Quota exceeded')
  expect(values.size).toBe(0)
  vi.stubGlobal('navigator', { ...navigator, locks: undefined })
  await expect(store.reserve('place', 'capture', request())).rejects.toThrow('No analysis was sent')
})

it('keeps an acknowledged intent recoverable when local deletion fails', async () => {
  const store = new AtlasClient(connection).analysisRecovery
  const pending = request()
  await store.reserve('place', 'capture', pending)
  vi.spyOn(localStorage, 'removeItem').mockImplementation(() => { throw new Error('Storage unavailable') })
  await expect(store.acknowledge('place', 'capture', pending.request_id)).rejects.toThrow('Storage unavailable')
  expect(await store.read('place', 'capture')).toEqual(pending)
})

it('retires only the withdrawn capture reference, leaving other captures intact', async () => {
  const store = new AtlasClient(connection).analysisRecovery
  const other = request()
  await store.reserve('place', 'capture', request())
  await store.reserve('place', 'other', other)
  await store.forgetRemoved('place', 'capture')
  expect(await store.read('place', 'capture')).toBeNull()
  expect(await store.read('place', 'other')).toEqual(other)
})

it.each(['post', 'status', 'directory'])('retires recovery after a verified removal %s response', async (mode) => {
  const receipt = { capture_id: 'capture', state: 'cleanup_pending', requested_at: 1, requested_by: 'workspace-operator', completed_at: null, recordings: 0, builds: 0, analysis_pending: true }
  const response = mode === 'directory' ? { receipts: [receipt], next_before: null, pending: 1, completed: 0, scope: 'space' } : receipt
  const client = new AtlasClient(connection, async () => new Response(JSON.stringify(response)))
  await client.analysisRecovery.reserve('place', 'capture', request())
  if (mode === 'post') await client.removeCapture('place', 'capture', 'a'.repeat(64))
  else if (mode === 'status') await client.removal('place', 'capture')
  else await client.removals('place')
  expect(await client.analysisRecovery.read('place', 'capture')).toBeNull()
})

it('does not discard a pending reference for an unverified removal or mask a confirmed removal when local storage fails', async () => {
  const receipt = { capture_id: 'capture', state: 'cleanup_pending', requested_at: 1, requested_by: 'workspace-operator', completed_at: null, recordings: 0, builds: 0, analysis_pending: true }
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ ...receipt, capture_id: 'wrong' })))
    .mockResolvedValueOnce(new Response(JSON.stringify(receipt)))
  const client = new AtlasClient(connection, fetch)
  const pending = request()
  await client.analysisRecovery.reserve('place', 'capture', pending)
  await expect(client.removal('place', 'capture')).rejects.toThrow('could not be verified')
  expect(await client.analysisRecovery.read('place', 'capture')).toEqual(pending)
  vi.spyOn(localStorage, 'removeItem').mockImplementation(() => { throw new Error('Storage blocked') })
  expect(await client.removeCapture('place', 'capture', 'a'.repeat(64))).toEqual(receipt)
  expect(await client.analysisRecovery.read('place', 'capture')).toEqual(pending)
})
