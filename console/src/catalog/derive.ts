import type { ConnectionStatus, ControlState } from '../control/state'
import { isLinkUp, type Tone } from '../shell/derive'
import type { BundleRef, CaptureRecord, GenerationJobState, RoomCaptureStatus } from './types'

export interface CatalogLink {
  up: boolean
  status: ConnectionStatus
  /** Sentence shown above a module while the console link is not connected. */
  notice: string | null
}

/**
 * Catalog data travels over the console connection. When that link is down the
 * last snapshot stays on screen, marked as such, and nothing can be sent.
 */
export function deriveCatalogLink(
  state: ControlState,
  subject: string,
  blocked: string,
): CatalogLink {
  const status = state.connection.status
  const up = isLinkUp(status)
  if (status === 'connected') return { up, status, notice: null }
  if (status === 'degraded') {
    return { up, status, notice: `The console connection is degraded. ${subject} may lag the relay.` }
  }
  return {
    up,
    status,
    notice: `The console connection is ${status}. ${subject} shows the last snapshot received; ${blocked} until the relay reports connected.`,
  }
}

export function jobTone(state: GenerationJobState): Tone {
  if (state === 'succeeded') return 'ok'
  if (state === 'running' || state === 'queued' || state === 'uploading') return 'warn'
  if (state === 'failed' || state === 'timed_out') return 'danger'
  return 'muted'
}

export const JOB_SENTENCE: Record<GenerationJobState, string> = {
  failed: 'The generation service returned an error. The capture is preserved; retry uses the same bundle.',
  timed_out: 'The job exceeded the service timeout. The capture is preserved; retry uses the same bundle.',
  succeeded: 'The room world is generated and can be opened. Its source photos stay beside it.',
  running: 'The service is generating. The operator can start the next room while this runs.',
  queued: 'Waiting for the service to start the job.',
  uploading: 'Sending the accepted bundle to the World API.',
  draft: 'No bundle has been accepted for this room yet.',
}

export const ROOM_CAPTURE_LABEL: Record<RoomCaptureStatus, string> = {
  captured: 'captured',
  capturing: 'capturing',
  needs_retake: 'needs retake',
  not_captured: 'not captured',
}

export function roomCaptureTone(status: RoomCaptureStatus): Tone {
  if (status === 'captured') return 'ok'
  if (status === 'needs_retake') return 'warn'
  return 'muted'
}

export const MANUAL_PHOTOS_REQUIRED = 3

export function bundleLabel(bundle: BundleRef, manualPhotos: number): string {
  if (bundle.kind === 'manual_phone') return `manual phone fallback · ${manualPhotos} photos`
  return `${bundle.capture_id ?? 'capture unreported'} · ${bundle.kind}`
}

export function bundleImages(bundle: BundleRef, manualPhotos: number): number {
  if (bundle.kind === 'pano_360') return 1
  if (bundle.kind === 'reconstruct_8') return 8
  return manualPhotos
}

export function sameBundle(left: BundleRef | null, right: BundleRef | null): boolean {
  if (left === null || right === null) return left === right
  return left.kind === right.kind && left.capture_id === right.capture_id
}

/** Drone bundles for a room in catalog order, then the manual phone fallback. */
export function bundleOptions(captures: CaptureRecord[], roomId: string): BundleRef[] {
  const drone = captures
    .filter((capture) => capture.room_id === roomId)
    .map((capture) => ({ kind: capture.pattern, capture_id: capture.capture_id }))
  return [...drone, { kind: 'manual_phone', capture_id: null }]
}

/** Design's colour key for the states gallery: one tone per vocabulary value. */
export function vocabTone(value: string): Tone {
  if (['live', 'completed', 'succeeded', 'ready', 'pass', 'connected'].includes(value)) return 'ok'
  if (
    ['offline', 'degraded', 'stale', 'leaving', 'needs retake', 'queued', 'uploading', 'running'].includes(
      value,
    )
  ) {
    return 'warn'
  }
  if (['unreported', 'draft'].includes(value)) return 'muted'
  if (['disconnected', 'failed', 'timed_out', 'refused'].includes(value)) return 'danger'
  return 'ink'
}
