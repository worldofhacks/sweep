import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { ControlState } from '../../../control/state'
import { UNAVAILABLE_MAP_AUTHORING_CLIENT, type MapAuthoringClient } from './client'
import { currentWorldObservation, snapshotWorldObservation } from './observations'
import { clientFixture, draftFixture, revision, rosterFixture } from './test-fixtures'
import type { MapDraft, MapRevision, WorldPositionObservation } from './types'
import { useWorldObservations } from './use-observations'

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(10_000) })
afterEach(() => { cleanup(); vi.useRealTimers() })

function sample(overrides: Partial<WorldPositionObservation> = {}): WorldPositionObservation {
  return {
    reference: { ...revision }, observationId: 'observation-1', sourceId: 'verified-test-source', deviceId: 11, connectionEpoch: 2,
    sessionId: 'test-session', frame: 'world', mapVersion: 'test-map-v1', floorId: 'test-floor',
    position: { x: 3, y: 4 }, tCapture: 9500, tIngest: 9600, confidence: 0.95,
    frameAssociationVerified: true, ...overrides,
  }
}

function observationClient() {
  const subscriptions: Array<{ receive: (value: WorldPositionObservation) => void; fail: (detail: string) => void; close: ReturnType<typeof vi.fn> }> = []
  const client = {
    ...clientFixture(), capabilities: ['observe'],
    subscribePositions: vi.fn((_request: { mapVersion: string; floorId: string }, receive: (value: WorldPositionObservation) => void, fail: (detail: string) => void) => {
      const close = vi.fn()
      subscriptions.push({ receive, fail, close })
      return close
    }),
  } satisfies MapAuthoringClient
  return { client, subscriptions, publish: (value = sample()) => act(() => subscriptions.at(-1)!.receive(value)) }
}

interface Props { client: MapAuthoringClient; draft: MapDraft; state: ControlState | undefined; reference?: MapRevision | null }
const renderOverlay = (initialProps: Props) => renderHook(({ client, draft, state, reference: requested = revision }: Props) => useWorldObservations(client, draft, state, Date.now, requested), { initialProps })

test('unsaved coordinate sources never open an observation subscription', () => {
  const source = observationClient()
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state: rosterFixture(), reference: null })
  expect(source.client.subscribePositions).not.toHaveBeenCalled()
  expect(result.current.positions).toEqual([])
})

test('duplicate map labels never move observations across exact saved bundle references', () => {
  const source = observationClient(), props = { client: source.client, draft: draftFixture(), state: rosterFixture() }
  const { result, rerender } = renderOverlay(props)
  source.publish()
  expect(result.current.positions).toHaveLength(1)
  const other = { ...revision, bundleId: 'same-label-other-bundle' }
  rerender({ ...props, reference: other })
  expect(source.subscriptions[0].close).toHaveBeenCalledOnce()
  source.publish()
  expect(result.current.positions).toEqual([])
  source.publish(sample({ reference: other }))
  expect(result.current.positions).toHaveLength(1)
})

test('retains detached frozen evidence and actual roster labels, without adopting provider mutations', () => {
  const source = observationClient(), value = sample()
  const { result, rerender } = renderOverlay({ client: source.client, draft: draftFixture(), state: rosterFixture() })
  source.publish(value)
  expect(source.client.subscribePositions).toHaveBeenCalledWith({ reference: revision, mapVersion: 'test-map-v1', floorId: 'test-floor' }, expect.any(Function), expect.any(Function))
  expect(result.current.positions[0].label).toBe('G-01')
  const retained = result.current.positions[0].observation
  expect(retained).not.toBe(value)
  expect(Object.isFrozen(retained)).toBe(true)
  expect(Object.isFrozen(retained.position)).toBe(true)
  expect(Object.isFrozen(retained.reference)).toBe(true)
  value.position.x = 99; value.observationId = 'provider-reused'; value.tCapture = 10_000
  value.reference.bundleId = 'provider-mutated-bundle'
  rerender({ client: source.client, draft: draftFixture(), state: rosterFixture() })
  expect(result.current.positions[0].observation).toMatchObject({ position: { x: 3, y: 4 }, observationId: 'observation-1', tCapture: 9500 })
  expect(result.current.positions[0].observation.reference).toEqual(revision)
  act(() => vi.advanceTimersByTime(500))
  expect(result.current.positions).toEqual([])
})

test.each(['map', 'floor', 'frame', 'transform', 'image', 'client', 'session', 'connection', 'epoch', 'membership', 'stale-device', 'capability'] as const)('permanently retires positions across %s A → B → A and rejects the old subscription', (change) => {
  const source = observationClient()
  const original: Props = { client: source.client, draft: draftFixture(), state: rosterFixture() }
  const { result, rerender } = renderOverlay(original)
  source.publish()
  expect(result.current.positions).toHaveLength(1)
  const oldSubscription = source.subscriptions[0]
  const next: Props = { ...original, draft: { ...original.draft, metadata: { ...original.draft.metadata } }, state: { ...original.state!, aircraft: { ...original.state!.aircraft } } }
  if (change === 'map') next.draft.metadata.mapVersion = 'other-map'
  if (change === 'floor') next.draft.metadata.floorId = 'other-floor'
  if (change === 'frame') next.draft.metadata.frame = 'unregistered'
  if (change === 'transform') next.draft.metadata.originXM = 1
  if (change === 'image') next.draft.image = { ...next.draft.image!, sha256: 'b'.repeat(64) }
  if (change === 'client') next.client = observationClient().client
  if (change === 'session') next.state!.sessionId = 'other-session'
  if (change === 'connection') next.state!.connection = { ...next.state!.connection, status: 'disconnected' }
  if (change === 'epoch') next.state!.aircraft[11] = { ...next.state!.aircraft[11], connection_epoch: 3 }
  if (change === 'membership') next.state!.aircraft[11] = { ...next.state!.aircraft[11], membership: 'disconnected' }
  if (change === 'stale-device') next.state!.aircraft[11] = { ...next.state!.aircraft[11], last_seen_at: 1 }
  if (change === 'capability') next.client = { ...source.client, capabilities: [] }
  rerender(next)
  expect(result.current.positions).toEqual([])
  expect(oldSubscription.close).toHaveBeenCalledOnce()
  rerender(original)
  expect(result.current.positions).toEqual([])
  act(() => {
    oldSubscription.receive(sample({ observationId: 'late-old-subscription', tCapture: 10_000, tIngest: 10_000 }))
    oldSubscription.fail('late old error')
  })
  expect(result.current.positions).toEqual([])
  expect(result.current.reason).not.toContain('late old error')
  source.publish(sample({ observationId: 'new-subscription', tCapture: 10_000, tIngest: 10_000 }))
  expect(result.current.positions[0].observation.observationId).toBe('new-subscription')
})

test.each(['capture', 'ingest', 'duplicate'] as const)('keeps the %s order cursor across stream errors, and recovers only with newer evidence', (order) => {
  const source = observationClient()
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state: rosterFixture() })
  source.publish()
  act(() => source.subscriptions[0].fail('Observation stream interrupted.'))
  expect(result.current.positions).toEqual([])
  expect(result.current.reason).toBe('Observation stream interrupted.')
  source.publish(sample({ observationId: order === 'duplicate' ? 'observation-1' : 'reordered',
    tCapture: order === 'capture' ? 9400 : 9500, tIngest: order === 'ingest' ? 9600 : 9700 }))
  expect(result.current.positions).toEqual([])
  expect(result.current.reason).toBe('Observation stream interrupted.')
  source.publish(sample({ observationId: 'newer', tCapture: 9800, tIngest: 9900 }))
  expect(result.current.positions[0].observation.observationId).toBe('newer')
  expect(result.current.reason).toContain('1 fresh')
})

test('expires each device at its own capture deadline, without a new provider frame or a second polling interval', () => {
  const source = observationClient(), state = rosterFixture()
  state.aircraft[12] = { ...state.aircraft[11], drone_id: 12, device_class: 'aircraft', unit: 4, connection_epoch: 7 }
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state })
  source.publish()
  source.publish(sample({ deviceId: 12, connectionEpoch: 7, observationId: 'aircraft-observation', tCapture: 9900, tIngest: 9950 }))
  expect(result.current.positions.map((item) => item.label)).toEqual(['G-01', 'D-04'])
  act(() => vi.advanceTimersByTime(499))
  expect(result.current.positions).toHaveLength(2)
  act(() => vi.advanceTimersByTime(1))
  expect(result.current.positions.map((item) => item.observation.deviceId)).toEqual([12])
  act(() => vi.advanceTimersByTime(399))
  expect(result.current.positions).toHaveLength(1)
  act(() => vi.advanceTimersByTime(1))
  expect(result.current.positions).toEqual([])
})

test('expires at exactly one second even when capture coincides with the initial timer origin', () => {
  const source = observationClient()
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state: rosterFixture() })
  source.publish(sample({ tCapture: 10_000, tIngest: 10_000 }))
  act(() => vi.advanceTimersByTime(999))
  expect(result.current.positions).toHaveLength(1)
  act(() => vi.advanceTimersByTime(1))
  expect(result.current.positions).toEqual([])
})

test('uses the accepted relay clock rather than comparing capture time with an offset browser clock', () => {
  const source = observationClient(), state = rosterFixture()
  vi.setSystemTime(20_000)
  state.lastStateEvent = { rosterVersion: state.rosterVersion, t: 10_000, receivedAt: 20_000, source: 'console' }
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state })
  source.publish()
  expect(result.current.positions).toHaveLength(1)
  act(() => vi.advanceTimersByTime(500))
  expect(result.current.positions).toEqual([])
})

test('removes a position when its device report expires before the position capture deadline', () => {
  const source = observationClient(), state = rosterFixture()
  state.aircraft[11] = { ...state.aircraft[11], last_seen_at: 5100 }
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state })
  source.publish(sample({ tCapture: 10_000, tIngest: 10_000 }))
  act(() => vi.advanceTimersByTime(100))
  expect(result.current.positions).toHaveLength(1)
  act(() => vi.advanceTimersByTime(1))
  expect(result.current.positions).toEqual([])
})

test('expired evidence cannot reappear after a clock restoration or duplicate replay', () => {
  const source = observationClient(), props = { client: source.client, draft: draftFixture(), state: rosterFixture() }
  const { result, rerender } = renderOverlay(props)
  source.publish()
  act(() => vi.advanceTimersByTime(500))
  expect(result.current.positions).toEqual([])
  vi.setSystemTime(10_000)
  rerender(props)
  source.publish()
  expect(result.current.positions).toEqual([])
})

test.each([
  { sessionId: 'other-session' }, { mapVersion: 'other-map' }, { floorId: 'other-floor' },
  { connectionEpoch: 3 }, { deviceId: 99 }, { frameAssociationVerified: false },
  { frame: 'body' }, { tCapture: 9000 }, { tCapture: 9999, tIngest: 10_001 },
  { tCapture: 9700, tIngest: 9600 }, { position: { x: Number.NaN, y: 0 } },
  { confidence: 2 }, { observationId: 12 }, { sourceId: null },
])('ignores invalid or unrelated provider evidence %j', (overrides) => {
  const source = observationClient()
  const { result } = renderOverlay({ client: source.client, draft: draftFixture(), state: rosterFixture() })
  const value = { ...sample(), ...overrides } as unknown as WorldPositionObservation
  expect(() => source.publish(value)).not.toThrow()
  expect(result.current.positions).toEqual([])
  expect(currentWorldObservation(value, draftFixture().metadata, rosterFixture(), 'test-session', Date.now(), revision)).toBe(false)
})

test('unavailable observation support never substitutes generic telemetry, and cleanup closes the stream and timer', () => {
  const source = observationClient(), props: Props = { client: UNAVAILABLE_MAP_AUTHORING_CLIENT, draft: draftFixture(), state: rosterFixture() }
  const { result, rerender, unmount } = renderOverlay(props)
  expect(result.current.positions).toEqual([])
  expect(result.current.reason).toContain('unavailable')
  rerender({ ...props, client: source.client })
  source.publish()
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(source.subscriptions[0].close).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
  expect(snapshotWorldObservation(null)).toBeNull()
})
