import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthoringOperation } from '../modules/map/authoring/client'
import { deferred, draftFixture } from '../modules/map/authoring/test-fixtures'
import type { MapRevision, WorldPositionObservation } from '../modules/map/authoring/types'
import type { NavigationCatalog, NavigationPreview, NavigationPreviewRequest } from '../navigation'
import { PlatformHttp, type PlatformFetch } from './http'
import { createHttpMapAuthoringClient } from './map-client'
import { HttpNavigationClient } from './navigation-client'
import { PlatformRuntime } from './runtime'

const connection = { baseUrl: 'wss://relay.test/internal/ws', sessionId: 'test-session', token: 'isolated-test-token' }
const reference: MapRevision = { bundleId: 'map-fixture', revision: '1', contentHash: 'a'.repeat(64) }
const otherReference: MapRevision = { ...reference, revision: '2', contentHash: 'b'.repeat(64) }
const operations: readonly AuthoringOperation[] = ['list', 'load', 'save', 'validate', 'approve', 'compare', 'record', 'observe', 'activate']
const SERVER_NOW = 1_000_000

function json(value: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json', ...headers } })
}

async function flush(): Promise<void> {
  for (let i = 0; i < 30; i += 1) await Promise.resolve()
}

function capability(ops: readonly string[] = operations, review = true) {
  return { v: 1, sessionId: connection.sessionId, mapAuthoring: { operations: [...ops] }, navigation: { review, dispatch: false } }
}

function catalog(): NavigationCatalog {
  return {
    session: connection.sessionId, catalogVersion: 'catalog-1', receivedAt: SERVER_NOW, expiresAt: SERVER_NOW + 15_000,
    map: {
      mapId: reference.bundleId, floorId: 'test-floor', frame: 'world', accepted: true, approvalId: 'approval-1',
      mapPin: { version: 'test-map-v1', contentSha256: reference.contentHash },
      geometryPin: { version: 'static-1', contentSha256: 'b'.repeat(64) },
      navigationPin: { version: 'catalog-1', contentSha256: 'c'.repeat(64) },
    },
    configVersion: 'config-1', motionConfig: { ground: { maximumSpeedMps: 0.3 } },
    destinations: [{ zoneId: 'lobby', name: 'Lobby', aliases: ['Entry'], floorId: 'test-floor', excluded: false, reachability: 'unknown', allowedClasses: ['aircraft', 'ground_vehicle'] }],
  }
}

function previewRequest(accepted = catalog()): NavigationPreviewRequest {
  return {
    session: connection.sessionId, intentId: 'intent-1', zoneId: 'lobby', rosterVersion: 7,
    selected: [{ id: 11, deviceClass: 'ground_vehicle', epoch: 2 }],
    catalogVersion: accepted.catalogVersion, map: accepted.map, configVersion: accepted.configVersion,
    motionConfig: accepted.motionConfig,
  }
}

function preview(request = previewRequest(), accepted = catalog()): NavigationPreview {
  return {
    previewId: 'preview-1', session: request.session, intentId: request.intentId, rosterVersion: request.rosterVersion,
    selected: request.selected, destination: accepted.destinations[0], map: request.map,
    catalogVersion: request.catalogVersion, configVersion: request.configVersion, motionConfig: request.motionConfig,
    routes: [], outcomes: request.selected.map((target) => ({ target, status: 'refused', code: 'class_planner_unavailable', detail: 'Isolated contract refusal fixture.' })),
    receivedAt: SERVER_NOW, expiresAt: SERVER_NOW + 10_000, dispatchEligible: false,
  }
}

function observation(): WorldPositionObservation {
  return {
    reference: { ...reference }, observationId: 'observation-1', sourceId: 'qualified-test-source', deviceId: 11, connectionEpoch: 2,
    sessionId: connection.sessionId, frame: 'world', mapVersion: 'test-map-v1', floorId: 'test-floor',
    position: { x: 1, y: 2 }, tCapture: 900, tIngest: 950, confidence: 0.9, frameAssociationVerified: true,
  }
}

function mapClient(fetcher: PlatformFetch) {
  const client = createHttpMapAuthoringClient(new PlatformHttp(connection, fetcher), operations)
  if (client.status !== 'available') throw new Error('Expected isolated available provider')
  return client
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('platform HTTP transport', () => {
  it('invokes native fetch on its global receiver, not on the HTTP client', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(function (this: unknown) {
      if (this !== globalThis) throw new TypeError('Illegal invocation')
      return Promise.resolve(json({ ready: true }))
    })
    await expect(new PlatformHttp(connection).request('/platform')).resolves.toEqual({ ready: true })
  })

  it('pins the connection and sends scoped bearer requests without cookies, caching, or redirects', async () => {
    const mutable = { ...connection, sessionId: 'room a' }
    const fetcher = vi.fn<PlatformFetch>().mockResolvedValue(json({ ready: true }))
    const http = new PlatformHttp(mutable, fetcher)
    mutable.token = 'replaced'
    await expect(http.request('/maps/load', { reference })).resolves.toEqual({ ready: true })
    expect(Object.isFrozen(http.connection)).toBe(true)
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('https://relay.test/internal/ws/api/sessions/room%20a/maps/load')
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error', headers: { Authorization: 'Bearer isolated-test-token', 'Content-Type': 'application/json' } })
    expect(JSON.parse(init!.body as string)).toEqual({ reference })
    expect(url).not.toContain(connection.token)
  })

  it('uses GET for discovery and rejects an invalid origin before fetching', async () => {
    const fetcher = vi.fn<PlatformFetch>().mockResolvedValue(json({ ready: true }))
    await new PlatformHttp(connection, fetcher).request('/platform')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'GET' })
    expect(fetcher.mock.calls[0][1]?.body).toBeUndefined()
    await expect(new PlatformHttp({ ...connection, baseUrl: 'file:///secret' }, fetcher).request('/platform')).rejects.toThrow(/URL/)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('bounds declared and streamed response bytes and rejects invalid UTF-8', async () => {
    const declared = new PlatformHttp(connection, async () => json({}, 200, { 'Content-Length': String(16 * 1024 * 1024 + 1) }))
    await expect(declared.request('/platform')).rejects.toThrow(/size limit/)
    const streamed = new PlatformHttp(connection, async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(16 * 1024 * 1024 + 1)); controller.close() },
    })))
    await expect(streamed.request('/platform')).rejects.toThrow(/size limit/)
    const invalidUtf8 = new PlatformHttp(connection, async () => new Response(new Uint8Array([0xc3, 0x28])))
    await expect(invalidUtf8.request('/platform')).rejects.toThrow()
  })

  it('reports bounded server refusals and hides malformed oversized error details', async () => {
    await expect(new PlatformHttp(connection, async () => json({ detail: 'Reload the current revision.' }, 409)).request('/maps/save', {})).rejects.toThrow('Reload the current revision.')
    await expect(new PlatformHttp(connection, async () => json({ detail: 'x'.repeat(2049) }, 503)).request('/platform')).rejects.toThrow('Relay service unavailable (503).')
    await expect(new PlatformHttp(connection, async () => new Response(null, { status: 204 })).request('/platform')).rejects.toThrow(/empty response/)
  })

  it('aborts stalled requests at the deadline and propagates caller cancellation', async () => {
    const fetcher = vi.fn<PlatformFetch>((_url, init) => new Promise((_resolve, reject) => {
      init!.signal!.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')), { once: true })
    }))
    const http = new PlatformHttp(connection, fetcher)
    const timed = expect(http.request('/platform')).rejects.toThrow('Cancelled')
    await vi.advanceTimersByTimeAsync(10_000)
    await timed
    const controller = new AbortController()
    const cancelled = expect(http.request('/platform', undefined, controller.signal)).rejects.toThrow('Cancelled')
    controller.abort()
    await cancelled
    expect(fetcher.mock.calls.every(([, init]) => init!.signal!.aborted)).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('immutable platform discovery', () => {
  it('retains providers for unchanged capability sets and replaces them on capability loss', async () => {
    let advertised = capability(['load', 'list'])
    const fetcher = vi.fn<PlatformFetch>(async () => json(advertised))
    const runtime = new PlatformRuntime(connection, fetcher)
    const listener = vi.fn()
    const stop = runtime.subscribe(listener)
    await flush()
    const original = runtime.getSnapshot()
    expect(original.mapAuthoring?.status).toBe('available')
    expect(Object.isFrozen(original)).toBe(true)
    advertised = capability(['list', 'load'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(runtime.getSnapshot()).toBe(original)
    expect(listener).toHaveBeenCalledTimes(1)
    advertised = capability(['list'], false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(runtime.getSnapshot()).not.toBe(original)
    expect(runtime.getSnapshot().mapAuthoring).not.toBe(original.mapAuthoring)
    expect(runtime.getSnapshot().navigation?.getSnapshot().status).toBe('unavailable')
    const next = runtime.getSnapshot().mapAuthoring
    if (!next || next.status !== 'available') throw new Error('Missing isolated map provider')
    expect(next.capabilities).toEqual(['list'])
    expect(Object.isFrozen(next.capabilities)).toBe(true)
    stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['session', 'duplicate', 'unknown'] as const)('retires providers when discovery has invalid %s evidence', async (kind) => {
    let response: unknown = capability()
    const runtime = new PlatformRuntime(connection, async () => json(response))
    const stop = runtime.subscribe(() => {})
    await flush()
    const previous = runtime.getSnapshot()
    response = kind === 'session' ? { ...capability(), sessionId: 'other-session' }
      : kind === 'duplicate' ? capability(['list', 'list']) : capability(['fictional-operation'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(runtime.getSnapshot()).not.toBe(previous)
    expect(runtime.getSnapshot().mapAuthoring?.status).toBe('unavailable')
    expect(runtime.getSnapshot().navigation?.getSnapshot().catalog).toBeNull()
    stop()
  })

  it('suppresses a late discovery response after cancellation and can subscribe again', async () => {
    const pending = deferred<Response>()
    const fetcher = vi.fn<PlatformFetch>().mockReturnValueOnce(pending.promise).mockResolvedValueOnce(json(capability(['list'])))
    const runtime = new PlatformRuntime(connection, fetcher)
    const listener = vi.fn()
    const stop = runtime.subscribe(listener)
    stop()
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
    pending.resolve(json(capability()))
    await flush()
    expect(listener).not.toHaveBeenCalled()
    const stopAgain = runtime.subscribe(listener)
    await flush()
    expect(listener).toHaveBeenCalledTimes(1)
    const client = runtime.getSnapshot().mapAuthoring
    expect(client?.status === 'available' && client.capabilities).toEqual(['list'])
    stopAgain()
  })
})

describe('HTTP map revision and observation bindings', () => {
  it('serializes only immutable reference identities for save, load, validation, approval and activation', async () => {
    const requests: Array<{ path: string; body: unknown }> = []
    const validation = { reference, validationId: 'validation-1', valid: true, issues: [] }
    const approval = { reference, validationId: 'validation-1', auditId: 'approval-1', approvedBy: 'console', approvedAt: 1000 }
    const fetcher = vi.fn<PlatformFetch>(async (input, init) => {
      const path = String(input).split('/test-session')[1]
      requests.push({ path, body: init?.body ? JSON.parse(init.body as string) : undefined })
      if (path === '/maps/load') return json({ reference, draft: draftFixture() })
      if (path === '/maps/validate') return json(validation)
      if (path === '/maps/approve') return json(approval)
      if (path === '/navigation/select-map') return json({ reference, selectionId: 'activation-1', selectedBy: 'console', selectedAt: 1000 })
      if (path === '/maps/save') return json(otherReference)
      return json(reference)
    })
    const client = mapClient(fetcher)
    const requested = { ...reference, untrustedApproval: true }
    expect(await client.save(draftFixture(), requested)).toEqual(otherReference)
    expect((await client.load(requested)).reference).toEqual(reference)
    expect(await client.validate(requested)).toEqual(validation)
    expect(await client.approve(requested, 'validation-1')).toEqual(approval)
    await client.selectForNavigation!(requested)
    expect(requests).toEqual([
      { path: '/maps/save', body: { draft: draftFixture(), expectedRevision: reference } },
      { path: '/maps/load', body: { reference } }, { path: '/maps/validate', body: { reference } },
      { path: '/maps/approve', body: { reference, validationId: 'validation-1' } },
      { path: '/navigation/select-map', body: { reference } },
    ])
  })

  it.each(['load', 'validate', 'approve', 'compare', 'activate'] as const)('rejects a valid-shaped %s response bound to another revision', async (operation) => {
    const responses = {
      load: { reference: otherReference, draft: draftFixture() },
      validate: { reference: otherReference, validationId: 'validation-1', valid: true, issues: [] },
      approve: { reference: otherReference, validationId: 'validation-1', auditId: 'approval-1', approvedBy: 'console', approvedAt: 1000 },
      compare: { left: otherReference, right: reference, changes: [] },
      activate: { reference: otherReference, selectionId: 'activation-1', selectedBy: 'console', selectedAt: 1000 },
    }
    const client = mapClient(async () => json(responses[operation]))
    const result = operation === 'load' ? client.load(reference) : operation === 'validate' ? client.validate(reference)
      : operation === 'approve' ? client.approve(reference, 'validation-1')
        : operation === 'compare' ? client.compare(reference, otherReference) : client.selectForNavigation!(reference)
    await expect(result).rejects.toThrow()
  })

  it('rejects malformed revisions, wrong validation receipts, and mismatched observation identities', async () => {
    await expect(mapClient(async () => json({ ...reference, contentHash: 'not-a-digest' })).save(draftFixture(), null)).rejects.toThrow(/revision/)
    await expect(mapClient(async () => json({ reference, validationId: 'different-validation', auditId: 'approval-1', approvedBy: 'console', approvedAt: 1000 })).approve(reference, 'validation-1')).rejects.toThrow()
    const request = { reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor', tagId: 7, deviceId: 11, connectionEpoch: 2 }
    for (const changed of [{ reference: { ...reference, bundleId: 'same-label-other-bundle' } }, { sessionId: 'other-session' }, { mapVersion: 'other-map' }, { floorId: 'other-floor' }, { deviceId: 12 }, { connectionEpoch: 3 }]) {
      await expect(mapClient(async () => json({ ...observation(), ...changed, tagId: 7 })).recordCurrentObservation(request)).rejects.toThrow()
    }
  })

  it('refuses stale save responses and oversized or duplicated revision lists', async () => {
    await expect(mapClient(async () => json(reference)).save(draftFixture(), reference)).rejects.toThrow(/new revision/)
    await expect(mapClient(async () => json({ ...otherReference, bundleId: 'another-bundle' })).save(draftFixture(), reference)).rejects.toThrow(/new revision/)
    const item = { ...reference, label: 'Fixture revision' }
    await expect(mapClient(async () => json([item, item])).list()).rejects.toThrow(/duplicate/)
    await expect(mapClient(async () => json(Array.from({ length: 257 }, (_, index) => ({ ...item, revision: String(index + 1) })))).list()).rejects.toThrow(/invalid revision/)
  })

  it('cancels position polling without delivering late results or errors', async () => {
    const pending = deferred<Response>()
    const fetcher = vi.fn<PlatformFetch>().mockReturnValue(pending.promise)
    const client = mapClient(fetcher)
    const received = vi.fn(), failed = vi.fn()
    const stop = client.subscribePositions!({ reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor' }, received, failed)
    stop()
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
    pending.resolve(json({ reference, observations: [observation()] }))
    await flush()
    await vi.advanceTimersByTimeAsync(1000)
    expect(received).not.toHaveBeenCalled()
    expect(failed).not.toHaveBeenCalled()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('rejects oversized observation batches without partial delivery', async () => {
    const client = mapClient(async () => json({ reference, observations: Array.from({ length: 65 }, observation) }))
    const received = vi.fn(), failed = vi.fn()
    const stop = client.subscribePositions!({ reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor' }, received, failed)
    await flush()
    expect(received).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledWith(expect.stringMatching(/invalid position/))
    stop()
  })

  it.each(['sessionId', 'mapVersion', 'floorId'] as const)('rejects a mixed position batch containing another %s', async (field) => {
    const client = mapClient(async () => json({ reference, observations: [observation(), { ...observation(), observationId: 'other-observation', [field]: 'other-context' }] }))
    const received = vi.fn(), failed = vi.fn()
    const stop = client.subscribePositions!({ reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor' }, received, failed)
    await flush()
    expect(received).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledOnce()
    stop()
  })

  it('captures a position subscription request and detaches delivered observations', async () => {
    const fetcher = vi.fn<PlatformFetch>(async () => json({ reference, observations: [observation()] }))
    const client = mapClient(fetcher)
    const request = { reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor' }
    const received = vi.fn(), failed = vi.fn()
    const stop = client.subscribePositions!(request, received, failed)
    request.mapVersion = 'caller-mutated-map'
    request.reference.bundleId = 'caller-mutated-bundle'
    await flush()
    expect(Object.isFrozen(received.mock.calls[0][0].position)).toBe(true)
    expect(Object.isFrozen(received.mock.calls[0][0].reference)).toBe(true)
    await vi.advanceTimersByTimeAsync(500)
    expect(fetcher.mock.calls).toHaveLength(2)
    for (const [, init] of fetcher.mock.calls) {
      expect(JSON.parse(init!.body as string)).toEqual({ reference: { ...reference }, mapVersion: 'test-map-v1', floorId: 'test-floor' })
    }
    expect(failed).not.toHaveBeenCalled()
    stop()
  })

  it.each(['empty-batch', 'observation'] as const)('rejects a different bundle reference with matching labels in the %s', async (kind) => {
    const other = { ...reference, bundleId: 'same-label-other-bundle' }
    const client = mapClient(async () => json(kind === 'empty-batch'
      ? { reference: other, observations: [] }
      : { reference, observations: [{ ...observation(), reference: other }] }))
    const received = vi.fn(), failed = vi.fn()
    const stop = client.subscribePositions!({ reference, mapVersion: 'test-map-v1', floorId: 'test-floor' }, received, failed)
    await flush()
    expect(received).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledOnce()
    stop()
  })
})

describe('navigation transport evidence lifetime and identity', () => {
  it('deducts round-trip latency while translating server deadlines to the local clock', async () => {
    let localNow = 10_000
    const pending = deferred<Response>()
    const client = new HttpNavigationClient(new PlatformHttp(connection, () => pending.promise), () => localNow)
    const stop = client.subscribe(() => {})
    localNow = 10_250
    pending.resolve(json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW }))
    await flush()
    expect(client.getSnapshot().catalog).toMatchObject({ receivedAt: 10_250, expiresAt: 25_000 })
    stop()
  })

  it.each(['rollback', 'expired', 'future-server-receipt'] as const)('refuses catalog lifetime with %s', async (kind) => {
    let localNow = 10_000
    const pending = deferred<Response>()
    const client = new HttpNavigationClient(new PlatformHttp(connection, () => pending.promise), () => localNow)
    const stop = client.subscribe(() => {})
    localNow = kind === 'rollback' ? 9_999 : kind === 'expired' ? 25_001 : 10_100
    pending.resolve(json({ status: 'ready', catalog: catalog(), serverNowMs: kind === 'future-server-receipt' ? SERVER_NOW - 1 : SERVER_NOW }))
    await flush()
    expect(client.getSnapshot().status).toBe('unavailable')
    expect(client.getSnapshot().catalog).toBeNull()
    stop()
  })

  it('freezes returned previews and sends only their captured one-shot server confirmation identity', async () => {
    const request = previewRequest()
    const fetcher = vi.fn<PlatformFetch>(async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW })
      : String(input).endsWith('/preview') ? json({ preview: preview(request), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW }) : json({ previewId: 'preview-1', intentId: 'intent-1', status: 'refused', code: 'navigation_execution_unavailable', detail: 'Execution is unavailable.', dispatchEligible: false }))
    const client = new HttpNavigationClient(new PlatformHttp(connection, fetcher), () => 10_000)
    const stop = client.subscribe(() => {})
    await flush()
    const result = await client.requestPreview(request)
    expect(Object.isFrozen(result.selected[0])).toBe(true)
    expect(result.expiresAt).toBe(20_000)
    await client.confirmPreview(result)
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1]!.body as string)).toEqual({ previewId: 'preview-1', intentId: 'intent-1', previewHash: 'd'.repeat(64) })
    await expect(client.confirmPreview(result)).rejects.toThrow(/no longer current/)
    stop()
  })

  it('deducts preview RTT and binds against the request captured before caller mutation', async () => {
    let localNow = 10_000
    const pending = deferred<Response>()
    const sent = previewRequest()
    const fetcher = vi.fn<PlatformFetch>(async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW }) : pending.promise)
    const client = new HttpNavigationClient(new PlatformHttp(connection, fetcher), () => localNow)
    const stop = client.subscribe(() => {})
    await flush()
    const request = { ...sent }
    const waiting = client.requestPreview(request)
    request.intentId = 'caller-mutated-intent'
    localNow = 10_400
    pending.resolve(json({ preview: preview(sent), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW }))
    const result = await waiting
    expect(result.intentId).toBe(sent.intentId)
    expect(result.receivedAt).toBe(10_400)
    expect(result.expiresAt).toBe(20_000)
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1]!.body as string).intentId).toBe(sent.intentId)
    stop()
  })

  it('retains an identical catalog object on refresh without extending captured review lifetime', async () => {
    let localNow = 10_000
    let serverNow = SERVER_NOW
    const fetcher = vi.fn<PlatformFetch>(async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: { ...catalog(), receivedAt: serverNow, expiresAt: serverNow + 15_000 }, serverNowMs: serverNow })
      : json({ preview: preview(), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW }))
    const client = new HttpNavigationClient(new PlatformHttp(connection, fetcher), () => localNow)
    const stop = client.subscribe(() => {})
    await flush()
    const firstCatalog = client.getSnapshot().catalog
    const captured = await client.requestPreview(previewRequest())
    localNow += 2000; serverNow += 2000
    await vi.advanceTimersByTimeAsync(2000)
    expect(client.getSnapshot().catalog).toBe(firstCatalog)
    expect(client.getSnapshot().preview).toBe(captured)
    expect(captured.expiresAt).toBe(20_000)
    expect(firstCatalog?.expiresAt).toBe(25_000)
    stop()
  })

  it('keeps the newer review when preview responses complete out of order', async () => {
    const first = deferred<Response>(), second = deferred<Response>()
    let requests = 0
    const client = new HttpNavigationClient(new PlatformHttp(connection, async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW })
      : ++requests === 1 ? first.promise : second.promise), () => 10_000)
    const stop = client.subscribe(() => {})
    await flush()
    const old = expect(client.requestPreview(previewRequest())).rejects.toThrow(/changed/)
    const nextRequest = { ...previewRequest(), intentId: 'intent-2' }
    const next = client.requestPreview(nextRequest)
    second.resolve(json({ preview: { ...preview(nextRequest), previewId: 'preview-2' }, previewHash: 'e'.repeat(64), serverNowMs: SERVER_NOW }))
    expect((await next).previewId).toBe('preview-2')
    first.resolve(json({ preview: preview(), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW }))
    await old
    expect(client.getSnapshot().preview?.intentId).toBe('intent-2')
    stop()
  })

  it('rejects forged confirmation success and consumes the captured attempt', async () => {
    const fetcher = vi.fn<PlatformFetch>(async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW })
      : String(input).endsWith('/preview') ? json({ preview: preview(), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW })
        : json({ previewId: 'preview-1', intentId: 'intent-1', status: 'completed', code: 'ok', detail: 'Forged test outcome.', dispatchEligible: true }))
    const client = new HttpNavigationClient(new PlatformHttp(connection, fetcher), () => 10_000)
    const stop = client.subscribe(() => {})
    await flush()
    const captured = await client.requestPreview(previewRequest())
    await expect(client.confirmPreview(captured)).rejects.toThrow(/invalid confirmation/)
    await expect(client.confirmPreview(captured)).rejects.toThrow(/no longer current/)
    stop()
  })

  it.each(['session', 'intent', 'selection', 'roster', 'destination', 'config', 'map'] as const)('rejects a preview whose %s changed from its request', async (field) => {
    const request = previewRequest()
    const valid = preview(request)
    const different: NavigationPreview = field === 'session' ? { ...valid, session: 'another-session' }
      : field === 'intent' ? { ...valid, intentId: 'another-intent' }
        : field === 'selection' ? preview({ ...request, selected: [{ id: 12, deviceClass: 'ground_vehicle', epoch: 2 }] })
          : field === 'roster' ? { ...valid, rosterVersion: 8 }
            : field === 'destination' ? { ...valid, destination: { ...valid.destination, zoneId: 'other-zone' } }
              : field === 'config' ? { ...valid, configVersion: 'another-config' }
                : { ...valid, map: { ...valid.map, approvalId: 'another-approval' } }
    const client = new HttpNavigationClient(new PlatformHttp(connection, async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: catalog(), serverNowMs: SERVER_NOW })
      : json({ preview: different, previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW })), () => 10_000)
    const stop = client.subscribe(() => {})
    await flush()
    await expect(client.requestPreview(request)).rejects.toThrow()
    expect(client.getSnapshot().preview).toBeNull()
    stop()
  })

  it('rejects a late preview after the accepted catalog changes', async () => {
    let current = catalog()
    const pending = deferred<Response>()
    const client = new HttpNavigationClient(new PlatformHttp(connection, async (input) => String(input).endsWith('/catalog')
      ? json({ status: 'ready', catalog: current, serverNowMs: SERVER_NOW }) : pending.promise), () => 10_000)
    const stop = client.subscribe(() => {})
    await flush()
    const waiting = expect(client.requestPreview(previewRequest())).rejects.toThrow(/catalog.*changed/)
    current = { ...current, catalogVersion: 'catalog-2' }
    await vi.advanceTimersByTimeAsync(2000)
    pending.resolve(json({ preview: preview(), previewHash: 'd'.repeat(64), serverNowMs: SERVER_NOW }))
    await waiting
    expect(client.getSnapshot().catalog?.catalogVersion).toBe('catalog-2')
    expect(client.getSnapshot().preview).toBeNull()
    stop()
  })
})
