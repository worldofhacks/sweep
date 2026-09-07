import type { ControlState } from '../../../control/state'
import { observeDevice } from '../../../control/observation'
import type { MapDraft, MapRevision, WorldPositionObservation } from './types'
import { revisionIdentity, sameRevision, validRevision } from './client'

export const POSITION_FRESH_MS = 1000

export function observationClock(state: ControlState | undefined, localNow: number): number {
  const last = state?.lastStateEvent
  return last?.receivedAt === undefined ? localNow : last.t + Math.max(0, localNow - last.receivedAt)
}

function validObservation(value: unknown): value is WorldPositionObservation {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const observation = value as WorldPositionObservation
  const text = (candidate: unknown) => typeof candidate === 'string' && candidate.trim().length > 0
  return validRevision(observation.reference) && text(observation.observationId) && text(observation.sourceId) && text(observation.sessionId)
    && text(observation.mapVersion) && text(observation.floorId)
    && Number.isSafeInteger(observation.deviceId) && observation.deviceId > 0
    && Number.isSafeInteger(observation.connectionEpoch) && observation.connectionEpoch > 0
    && observation.frame === 'world' && observation.frameAssociationVerified === true
    && Number.isSafeInteger(observation.tCapture) && Number.isSafeInteger(observation.tIngest)
    && observation.tCapture >= 0 && observation.tCapture <= observation.tIngest
    && Number.isFinite(observation.position?.x) && Number.isFinite(observation.position?.y)
    && Number.isFinite(observation.confidence) && observation.confidence >= 0 && observation.confidence <= 1
}

/** Provider DTOs may be reused. Retain only detached, immutable evidence. */
export function snapshotWorldObservation(value: unknown): WorldPositionObservation | null {
  if (!validObservation(value)) return null
  return Object.freeze({
    reference: Object.freeze(revisionIdentity(value.reference)),
    observationId: value.observationId, sourceId: value.sourceId,
    deviceId: value.deviceId, connectionEpoch: value.connectionEpoch, sessionId: value.sessionId,
    frame: value.frame, mapVersion: value.mapVersion, floorId: value.floorId,
    position: Object.freeze({ x: value.position.x, y: value.position.y }),
    tCapture: value.tCapture, tIngest: value.tIngest, confidence: value.confidence,
    frameAssociationVerified: value.frameAssociationVerified,
  })
}

/** Display and record gates share current session, epoch, association and clock checks. */
export function currentWorldObservation(observation: WorldPositionObservation, metadata: MapDraft['metadata'], state: ControlState | undefined, session: string, localNow: number, reference: MapRevision | null): boolean {
  if (!validObservation(observation) || !state || state.sessionId !== session || !['connected', 'degraded'].includes(state.connection.status)) return false
  const at = observationClock(state, localNow)
  const device = state.aircraft[observation.deviceId]
  return Boolean(Number.isFinite(at) && device && device.connection_epoch === observation.connectionEpoch && observeDevice(device, at).state === 'current'
    && observation.frameAssociationVerified === true && metadata.frame === 'world' && observation.frame === 'world'
    && sameRevision(observation.reference, reference)
    && observation.sessionId === session && observation.mapVersion === metadata.mapVersion && observation.floorId === metadata.floorId
    && observation.tIngest <= at && at - observation.tCapture < POSITION_FRESH_MS)
}

/** Serialize image bytes only when the image object changes, never per fleet tick. */
export function coordinateImageIdentity(image: MapDraft['image']): string {
  return JSON.stringify(image && { dataUrl: image.dataUrl, sha256: image.sha256, width: image.width, height: image.height })
}

/** Tag and geometry edits retain this source; coordinate/image changes retire it. */
export function coordinateSourceIdentity(m: MapDraft['metadata'], imageIdentity: string): string {
  return JSON.stringify({
    frame: m.frame, floorId: m.floorId, mapVersion: m.mapVersion,
    resolutionM: m.resolutionM, originXM: m.originXM, originYM: m.originYM,
    units: m.units, registration: m.registration,
  }) + '\n' + imageIdentity
}
