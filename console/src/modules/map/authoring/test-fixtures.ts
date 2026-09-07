/** Isolated test inputs. This module is never imported by the operator runtime. */
import { vi } from 'vitest'
import { createInitialControlState } from '../../../control/state'
import { fixtureAircraft } from '../../../testing/fixture-relay-client'
import type { MapAuthoringClient } from './client'
import type { MapDraft, MapRevision, MapTag, MapValidation } from './types'

export const revision: MapRevision = { bundleId: 'test-bundle', revision: 'revision-1', contentHash: 'a'.repeat(64) }
export const nextRevision: MapRevision = { ...revision, revision: 'revision-2', contentHash: 'b'.repeat(64) }

export function rosterFixture() {
  const state = createInitialControlState('test-session', 10_000)
  state.selection = [11]
  state.connection = { ...state.connection, status: 'connected' }
  state.aircraft = { 11: { ...fixtureAircraft(10_000)[0], drone_id: 11, connection_epoch: 2, device_class: 'ground_vehicle', unit: 1 } }
  return state
}

export function draftFixture(): MapDraft {
  return {
    format: 'sweep-map-draft-v1',
    metadata: { mapVersion: 'test-map-v1', floorId: 'test-floor', frame: 'world', resolutionM: 0.1, originXM: 0, originYM: 0 },
    image: { name: 'test-only-map.png', dataUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAAAAABVicqIAAAAPUlEQVR4nO3NAQkAAAwDoEVf9LU4HLSA6YFIJBKJRCKRSCQSiUQikUgkEolEIpFIJBKJRCKRSCQSiUTyPxmMBYkeaGnfvwAAAABJRU5ErkJggg==', width: 100, height: 100, sha256: '5e3b3d0cce5194bab87a3d9d17d5520aff1cae6107a2f5b1925b85fdffe36e9f' },
    features: [{ id: 'boundary', kind: 'geofence', name: 'Test boundary', aliases: [], points: [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 0, y: 0 }], widthM: null, flightHeightM: null, heightToleranceM: null, heightEvidence: '' }],
    tags: [],
  }
}

export function tagFixture(): MapTag {
  return { id: 'tag-record', tagId: 7, family: 'tag36h11', sizeM: 0.2, position: { x: 2, y: 2 }, heightM: 0, source: 'surveyed', confidence: 0.9, observations: ['test-observation'], usedForFlight: true, tapeVerified: true, tapeEvidence: 'Test-only tape measurement record' }
}

export function clientFixture() {
  return {
    status: 'available' as const,
    sessionId: 'test-session',
    capabilities: ['list', 'load', 'save', 'validate', 'approve', 'compare', 'record'],
    list: vi.fn(async () => [{ ...revision, label: 'Test revision' }, { ...nextRevision, label: 'Second test revision' }]),
    load: vi.fn(async (reference: MapRevision) => ({ reference, draft: draftFixture() })),
    save: vi.fn(async () => revision),
    validate: vi.fn(async (reference: MapRevision): Promise<MapValidation> => ({ reference, validationId: 'test-validation', valid: true, issues: [] })),
    approve: vi.fn(async (reference: MapRevision, validationId: string) => ({ reference, validationId, auditId: 'test-audit', approvedBy: 'Test operator', approvedAt: 1000 })),
    compare: vi.fn(async (left: MapRevision, right: MapRevision) => ({ left, right, changes: [{ path: 'metadata.mapVersion', before: 'v1', after: 'v2' }] })),
    recordCurrentObservation: vi.fn(async () => ({ observationId: 'new-observation', sourceId: 'test-source', deviceId: 11, connectionEpoch: 2, sessionId: 'test-session', tagId: 7, frame: 'world' as const, mapVersion: 'test-map-v1', floorId: 'test-floor', position: { x: 3, y: 3 }, tCapture: 9500, tIngest: 9600, confidence: 0.95, frameAssociationVerified: true })),
  } satisfies MapAuthoringClient
}

export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
