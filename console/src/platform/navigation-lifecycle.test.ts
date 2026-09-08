import { expect, test, vi } from 'vitest'
import type { NavigationCatalog, NavigationPreviewRequest } from '../navigation'
import { PlatformHttp } from './http'
import { HttpNavigationClient } from './navigation-client'

const catalog: NavigationCatalog = {
  session: 'isolated-lifecycle-test', catalogVersion: 'catalog-1', receivedAt: 1000, expiresAt: 11000,
  map: {
    mapId: 'map-1', floorId: 'floor-1', frame: 'world', accepted: true, approvalId: 'approval-1',
    mapPin: { version: 'map-1', contentSha256: 'a'.repeat(64) },
    geometryPin: { version: 'static-1', contentSha256: 'b'.repeat(64) },
    navigationPin: { version: 'catalog-1', contentSha256: 'c'.repeat(64) },
  },
  configVersion: 'test-config', motionConfig: { isolatedTestValue: 1 },
  destinations: [{ zoneId: 'zone-1', name: 'Test zone', aliases: [], floorId: 'floor-1',
    excluded: false, reachability: 'unknown', allowedClasses: ['aircraft', 'ground_vehicle'] }],
}
const selected = [{ id: 1, deviceClass: 'ground_vehicle' as const, epoch: 1 }]

function request(intentId: string): NavigationPreviewRequest {
  return { session: catalog.session, intentId, zoneId: 'zone-1', rosterVersion: 1, selected,
    map: catalog.map, catalogVersion: catalog.catalogVersion, configVersion: catalog.configVersion,
    motionConfig: catalog.motionConfig }
}

function response(intentId: string, hash: string) {
  return { serverNowMs: 1000, previewHash: hash.repeat(64), preview: {
    previewId: `preview-${intentId}`, intentId, session: catalog.session, rosterVersion: 1,
    selected, destination: catalog.destinations[0], map: catalog.map,
    catalogVersion: catalog.catalogVersion, configVersion: catalog.configVersion,
    motionConfig: catalog.motionConfig, routes: [],
    outcomes: selected.map((target) => ({ target, status: 'refused', code: 'capability_disabled',
      detail: 'The isolated test profile does not advertise navigation execution.' })),
    receivedAt: 1000, expiresAt: 11000, dispatchEligible: false,
  } }
}

function deferred() {
  let resolve!: (value: unknown) => void
  const promise = new Promise<unknown>((done) => { resolve = done })
  return { promise, resolve }
}

test('an older HTTP preview cannot replace a newer captured review or its server hash', async () => {
  const old = deferred(), current = deferred()
  const http = new PlatformHttp({ baseUrl: 'ws://127.0.0.1:8010', sessionId: catalog.session, token: 'isolated-test-token' })
  const call = vi.spyOn(http, 'request').mockImplementation(async (path, body) => {
    if (path === '/navigation/catalog') return { status: 'ready', catalog, serverNowMs: 1000 }
    if (path === '/navigation/preview') return (body as NavigationPreviewRequest).intentId === 'old' ? old.promise : current.promise
    const confirmation = body as { previewId: string; intentId: string }
    return { ...confirmation, status: 'refused', code: 'navigation_execution_unavailable',
      detail: 'Class-qualified navigation execution is unavailable.', dispatchEligible: false }
  })
  const client = new HttpNavigationClient(http, () => 2000)
  const unsubscribe = client.subscribe(() => {})
  try {
    await vi.waitFor(() => expect(client.getSnapshot().status).toBe('ready'))
    const older = client.requestPreview(request('old'))
    const olderRefusal = expect(older).rejects.toThrow()
    const newer = client.requestPreview(request('new'))
    current.resolve(response('new', '2'))
    const preview = await newer
    old.resolve(response('old', '1'))
    await olderRefusal
    expect(client.getSnapshot().preview).toBe(preview)
    await client.confirmPreview(preview)
    expect(call).toHaveBeenLastCalledWith('/navigation/confirm', {
      previewId: 'preview-new', intentId: 'new', previewHash: '2'.repeat(64),
    })
  } finally { unsubscribe() }
})

test('unsubscribing retires an in-flight preview even if the catalog object remains identical', async () => {
  const pending = deferred()
  const http = new PlatformHttp({ baseUrl: 'ws://127.0.0.1:8010', sessionId: catalog.session, token: 'isolated-test-token' })
  vi.spyOn(http, 'request').mockImplementation(async (path) => path === '/navigation/catalog'
    ? { status: 'ready', catalog, serverNowMs: 1000 } : pending.promise)
  const client = new HttpNavigationClient(http, () => 2000)
  const unsubscribe = client.subscribe(() => {})
  await vi.waitFor(() => expect(client.getSnapshot().status).toBe('ready'))
  const result = client.requestPreview(request('retired'))
  const refusal = expect(result).rejects.toThrow()
  unsubscribe()
  pending.resolve(response('retired', '1'))
  await refusal
  expect(client.getSnapshot().preview).toBeNull()
})
