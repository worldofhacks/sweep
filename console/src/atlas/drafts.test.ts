import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { BrowserSpaceDraftStore, EMPTY_SPACE, parseDraft, type SpaceDraft, type SpaceDraftStore } from './drafts'
import { SpaceDraftSession } from './useSpaceDraft'
import { AtlasClient } from './client'

const connection = { baseUrl: 'https://relay.example', sessionId: 'Austin', token: 'private-key-one' }
function draft(): SpaceDraft { return { version: 1, id: crypto.randomUUID(), revision: 1, updatedAt: Date.now(),
  space: { ...EMPTY_SPACE, title: 'Shoal Creek access', latitude: 30.279, longitude: -97.748 },
  coordinates: ['30.279', '-97.748'], submitted: null } }
beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key),
    key: (index: number) => [...values.keys()][index], toJSON: () => Object.fromEntries(values) })
  // Browser integration separately checks the real cross-tab lock. Unit boundary executes atomically.
  vi.stubGlobal('navigator', { ...navigator, locks: { request: async (_key: string, action: () => unknown) => action() } })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('retains a partial draft through a fresh client, without storing credentials', async () => {
  const first = new BrowserSpaceDraftStore(connection)
  const value = { ...draft(), coordinates: ['', '-97.748'] as [string, string] }
  await first.write(value, null)
  expect(await new BrowserSpaceDraftStore(connection).read()).toEqual(value)
  expect(JSON.stringify(localStorage)).not.toContain(connection.token)
  for (const change of [{ token: 'new-key' }, { sessionId: 'other' }, { baseUrl: 'https://other.example' }]) {
    expect(await new BrowserSpaceDraftStore({ ...connection, ...change }).read()).toBeNull()
  }
})
it('refuses stale edits and deletions without losing the newer draft', async () => {
  const store = new BrowserSpaceDraftStore(connection)
  const first = draft(), latest = { ...first, revision: 2, space: { ...first.space, title: 'Updated Austin survey' } }
  await store.write(first, null)
  await store.write(latest, first)
  await expect(store.write({ ...first, revision: 2 }, first)).rejects.toThrow('another window')
  await expect(store.remove(first)).rejects.toThrow('another window')
  expect(await store.read()).toEqual(latest)
  await store.remove(latest)
  expect(await store.read()).toBeNull()
})
it('does not overwrite malformed or unsupported saved data', async () => {
  const store = new BrowserSpaceDraftStore(connection)
  await store.write(draft(), null)
  const key = localStorage.key(0)!
  localStorage.setItem(key, '{"version":99}')
  await expect(store.read()).rejects.toThrow('not been overwritten')
  await expect(store.write(draft(), null)).rejects.toThrow()
  expect(localStorage.getItem(key)).toBe('{"version":99}')
  expect(() => parseDraft({ ...draft(), coordinates: [null, ''] })).toThrow()
})
it('coalesces rapid edits, saves before freezing publication and recovers the exact submission', async () => {
  const store = new BrowserSpaceDraftStore(connection)
  const session = new SpaceDraftSession(store)
  await session.load(); session.start()
  session.edit({ ...EMPTY_SPACE, title: 'First' })
  session.edit({ ...EMPTY_SPACE, title: 'Austin final report' }, ['30.279', '-97.748'])
  await session.flush()
  expect(session.getSnapshot().saving).toBe(false)
  const saved = await session.submission()
  expect(saved.submitted?.title).toBe('Austin final report')
  session.edit({ ...EMPTY_SPACE, title: 'Must not change' })
  expect(session.getSnapshot().draft?.space.title).toBe('Austin final report')
  const reopened = new SpaceDraftSession(new BrowserSpaceDraftStore(connection))
  await reopened.load()
  expect(await reopened.submission()).toEqual(saved)
  await reopened.discard()
  expect(await store.read()).toBeNull()
})
it('retains unsaved edits after storage failure and never calls publication without durable storage', async () => {
  const store = new BrowserSpaceDraftStore(connection)
  const write = vi.spyOn(store, 'write').mockRejectedValue(new Error('Storage full'))
  const session = new SpaceDraftSession(store)
  await session.load(); session.start()
  session.edit({ ...EMPTY_SPACE, title: 'Keep this report' })
  await expect(session.submission()).rejects.toThrow('Storage full')
  expect(session.getSnapshot()).toMatchObject({ saving: false, error: 'Storage full', draft: { space: { title: 'Keep this report' }, submitted: null } })
  write.mockRestore()
  await session.flush()
  expect((await store.read())?.space.title).toBe('Keep this report')
})
it('waits for an in-flight edit before deletion so a late save cannot resurrect a discarded draft', async () => {
  let release!: () => void
  const base = new BrowserSpaceDraftStore(connection)
  const slow: SpaceDraftStore = { ...base, read: () => base.read(), remove: previous => base.remove(previous),
    write: async (value, previous) => { await new Promise<void>(resolve => { release = resolve }); await base.write(value, previous) } }
  const session = new SpaceDraftSession(slow)
  await session.load(); session.start()
  const discard = session.discard()
  release(); await discard
  expect(await base.read()).toBeNull()
})
it('uses only the idempotent endpoint and refuses an unconfirmed response without a create fallback', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ space: { id: 'space' } })))
  const client = new AtlasClient(connection, fetch)
  const value = draft()
  await expect(client.publishDraft(value.id, value.space)).rejects.toThrow('not confirmed')
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toContain(`/drafts/${value.id}/publish`)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(value.space)
})
