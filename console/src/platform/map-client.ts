import { activationReceipt, approvalReceipt, revisionComparison, revisionIdentity, revisionList, sameRevision, validRevision, validationReceipt, type AuthoringOperation, type MapAuthoringClient } from '../modules/map/authoring/client'
import { parseLocalDraft } from '../modules/map/authoring/files'
import { snapshotWorldObservation } from '../modules/map/authoring/observations'
import type { CurrentTagObservation, MapRevision } from '../modules/map/authoring/types'
import { isRecord, PlatformHttp } from './http'

export function createHttpMapAuthoringClient(http: PlatformHttp, operations: readonly AuthoringOperation[]): MapAuthoringClient {
  const reference = (raw: unknown): MapRevision => {
    if (!validRevision(raw)) throw new Error('The relay returned an invalid map revision.')
    return revisionIdentity(raw)
  }
  const match = (actual: MapRevision, expected: MapRevision) => {
    if (!sameRevision(actual, expected)) throw new Error('The relay returned evidence for another map revision.')
  }
  return Object.freeze({
    status: 'available', sessionId: http.connection.sessionId, capabilities: Object.freeze([...operations]),
    list: async () => revisionList(await http.request('/maps/revisions')),
    load: async (requested: MapRevision) => {
      const expected = revisionIdentity(requested)
      const value = await http.request('/maps/load', { reference: expected })
      if (!isRecord(value)) throw new Error('The relay returned an invalid saved map.')
      const actual = reference(value.reference)
      match(actual, expected)
      return { reference: actual, draft: parseLocalDraft(JSON.stringify(value.draft)) }
    },
    save: async (draft, expectedRevision) => {
      const expected = expectedRevision && revisionIdentity(expectedRevision)
      const actual = reference(await http.request('/maps/save', { draft, expectedRevision: expected }))
      if (expected && (actual.bundleId !== expected.bundleId || sameRevision(actual, expected))) throw new Error('The relay did not create the requested new revision.')
      return actual
    },
    validate: async (ref) => {
      const expected = revisionIdentity(ref)
      const result = validationReceipt(await http.request('/maps/validate', { reference: expected }))
      match(result.reference, expected)
      return result
    },
    approve: async (ref, validationId) => {
      const expected = revisionIdentity(ref)
      const result = approvalReceipt(await http.request('/maps/approve', { reference: expected, validationId }))
      match(result.reference, expected)
      if (result.validationId !== validationId) throw new Error('The relay approved another validation receipt.')
      return result
    },
    compare: async (left, right) => {
      const expectedLeft = revisionIdentity(left), expectedRight = revisionIdentity(right)
      const result = revisionComparison(await http.request('/maps/compare', { left: expectedLeft, right: expectedRight }))
      match(result.left, expectedLeft); match(result.right, expectedRight)
      return result
    },
    selectForNavigation: async (ref) => {
      const expected = revisionIdentity(ref)
      const result = activationReceipt(await http.request('/navigation/select-map', { reference: expected }))
      match(result.reference, expected)
      return result
    },
    recordCurrentObservation: async (request) => {
      const expected = { ...request, reference: revisionIdentity(request.reference) }
      const result = await http.request('/maps/record', expected)
      if (!isRecord(result) || result.tagId !== expected.tagId) throw new Error('The relay returned an invalid tag observation.')
      const observation = snapshotWorldObservation(result)
      if (!observation || !sameRevision(observation.reference, expected.reference) || observation.sessionId !== http.connection.sessionId || observation.mapVersion !== expected.mapVersion
        || observation.floorId !== expected.floorId || observation.deviceId !== expected.deviceId
        || observation.connectionEpoch !== expected.connectionEpoch || !observation.frameAssociationVerified) throw new Error('The relay returned an invalid tag observation.')
      return { ...observation, tagId: result.tagId } as CurrentTagObservation
    },
    subscribePositions: (request, onObservation, onError) => {
      const expected = { ...request, reference: revisionIdentity(request.reference) }
      let stopped = false
      let timer: ReturnType<typeof setTimeout> | undefined
      const controller = new AbortController()
      const poll = async () => {
        try {
          const value = await http.request('/maps/positions', expected, controller.signal)
          if (!isRecord(value) || !validRevision(value.reference) || !sameRevision(value.reference, expected.reference) || !Array.isArray(value.observations) || value.observations.length > 64) throw new Error('The relay returned invalid position observations.')
          const observations = value.observations.map(snapshotWorldObservation)
          if (observations.some((observation) => !observation || !sameRevision(observation.reference, expected.reference) || observation.sessionId !== http.connection.sessionId
            || observation.mapVersion !== expected.mapVersion || observation.floorId !== expected.floorId
            || !observation.frameAssociationVerified)) throw new Error('The relay returned invalid position observations.')
          if (!stopped) for (const observation of observations) if (observation) onObservation(observation)
        } catch (error) {
          if (!stopped) onError(error instanceof Error ? error.message : 'Position observations are unavailable.')
        } finally { if (!stopped) timer = setTimeout(() => void poll(), 500) }
      }
      void poll()
      return () => { stopped = true; controller.abort(); clearTimeout(timer) }
    },
  } satisfies MapAuthoringClient)
}
