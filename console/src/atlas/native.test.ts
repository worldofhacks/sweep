import { afterEach, expect, it, vi } from 'vitest'
import { NativeAtlasClient, nativeCall, nativeFetch, type NativeSession } from './native'
import type { SpaceDetail } from './types'
import { EMPTY_SPACE } from './drafts'

const session: NativeSession = { id: 'credential-one', baseUrl: 'http://192.168.1.20:8000/root', sessionId: 'room', space: null }
function port(handler: (op: string, payload: Record<string, unknown>) => unknown) {
  const api = { onmessage: null as ((event: { data: string }) => void) | null, postMessage: vi.fn((raw: string) => {
    const message = JSON.parse(raw)
    queueMicrotask(() => {
      try { api.onmessage?.({ data: JSON.stringify({ id: message.id, result: handler(message.op, message.payload) }) }) }
      catch (error) { api.onmessage?.({ data: JSON.stringify({ id: message.id, error: (error as Error).message, code: (error as { code?: string }).code }) }) }
    })
  }) }
  window.SweepAtlasNative = api
  return api
}
afterEach(() => { delete window.SweepAtlasNative; vi.restoreAllMocks() })

it('fails clearly outside the Android app', async () => {
  await expect(nativeCall('getSession')).rejects.toThrow('Android app')
})
it('correlates replies and binds JSON requests to an immutable credential without a bearer', async () => {
  const api = port(() => ({ status: 200, body: '{"spaces":[]}' }))
  const client = new NativeAtlasClient(session)
  expect(await client.list()).toEqual([])
  const sent = JSON.parse(api.postMessage.mock.calls[0][0])
  expect(sent).toMatchObject({ op: 'request', payload: { session: 'credential-one', path: '/atlas/spaces', method: 'GET' } })
  expect(sent.payload).not.toHaveProperty('token')
  expect(client.connection.token).toBe('')
})
it('never sends non-Atlas requests or another workspace through the bridge', async () => {
  const api = port(() => null)
  const fetcher = nativeFetch(session)
  await expect(fetcher('http://192.168.1.20:8000/root/api/sessions/room/control')).rejects.toThrow('outside Atlas')
  await expect(fetcher('http://192.168.1.20:8000/root/api/sessions/other/atlas/spaces')).rejects.toThrow('outside Atlas')
  expect(api.postMessage).not.toHaveBeenCalled()
})
it('streams binary media through the same-origin native interceptor, with no key in URLs', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('media'))
  const api = port(() => null)
  await nativeFetch(session)('http://192.168.1.20:8000/root/api/sessions/room/atlas/spaces/place/captures/photo/media')
  expect(fetch).toHaveBeenCalledWith('https://appassets.androidplatform.net/atlas-data/credential-one/atlas/spaces/place/captures/photo/media',
    expect.objectContaining({ credentials: 'omit', redirect: 'error' }))
  expect(api.postMessage).not.toHaveBeenCalled()
})
it('uses cached space metadata only for a network failure, and never shows stale live people', async () => {
  const offline = vi.fn()
  const cached = { space: { id: 'place' }, people: [{ name: 'Stale person' }] } as unknown as SpaceDetail
  port(op => {
    if (op === 'cachedSpaces') return [cached]
    throw Object.assign(new Error('No connection'), { code: 'network' })
  })
  const detail = await new NativeAtlasClient(session, offline).detail('place')
  expect(detail.space.id).toBe('place')
  expect(detail.people).toEqual([])
  expect(offline).toHaveBeenCalledWith(true)
})
it('does not use the offline cache after invitation revocation', async () => {
  const api = port(() => ({ status: 403, body: '{"detail":"Invitation revoked"}' }))
  await expect(new NativeAtlasClient(session).detail('place')).rejects.toThrow('Invitation revoked')
  expect(api.postMessage.mock.calls.map(call => JSON.parse(call[0]).op)).toEqual(['request'])
})
it('does not update a closed screen when a native request completes after cancellation', async () => {
  port(() => ({ status: 200, body: '{"spaces":[]}' }))
  const controller = new AbortController()
  const result = new NativeAtlasClient(session).list(controller.signal)
  controller.abort()
  await expect(result).rejects.toHaveProperty('name', 'AbortError')
})
it('keeps drafts in the native vault and binds every action to the original credential', async () => {
  const draft = { version: 1, id: crypto.randomUUID(), revision: 1, updatedAt: Date.now(),
    space: EMPTY_SPACE, coordinates: ['', ''], submitted: null }
  const api = port(op => op === 'readDraft' ? draft : true)
  const client = new NativeAtlasClient(session)
  expect(await client.drafts.read()).toEqual(draft)
  await client.drafts.write({ ...draft, version: 1, coordinates: ['', ''], revision: 2 }, await client.drafts.read())
  await client.drafts.remove((await client.drafts.read())!)
  const calls = api.postMessage.mock.calls.map(([raw]) => JSON.parse(raw))
  expect(calls.every(value => value.payload.session === 'credential-one')).toBe(true)
  expect(calls.map(value => value.op)).toEqual(['readDraft', 'readDraft', 'writeDraft', 'readDraft', 'removeDraft'])
  expect(calls.some(value => value.op === 'request')).toBe(false)
})
