import { membershipWord, motionObservationCurrent, observationCurrent } from '../../control/observation'
import type { RequestRecord } from '../../control/state'
import type { DroneId, MediaStreamState, MediaStreamStatus, RelayAircraftState } from '../../relay/contract'
import type { Tone } from '../../shell/derive'
import { humanizeCode } from '../../shell/format'

/** Stream words and colours from the Sweep Console v4 design, Live surface. */
export interface StreamView {
  status: MediaStreamStatus
  tone: Tone
  lastFrame: string
  degraded: boolean
  degradedWord: string
}

export const DEGRADED_WORD: Record<Exclude<MediaStreamStatus, 'live'>, string> = {
  offline: 'No video. The adapter reports the stream offline.',
  unreported: 'No video reported. The console shows unreported rather than inventing a state.',
}

export const STREAM_TONE: Record<MediaStreamStatus, Tone> = {
  live: 'ok',
  offline: 'warn',
  unreported: 'muted',
}

/** "just now" under a second, otherwise whole seconds, as the design's ago(). */
export function formatAge(ageMs: number): string {
  const seconds = Math.round(Math.max(0, ageMs) / 1000)
  return seconds < 1 ? 'just now' : `${seconds} s ago`
}

export const VIDEO_FRESH_MS = 5_000

export function deriveStream(drone: RelayAircraftState, at: number, camera?: MediaStreamState | null): StreamView {
  const now = drone.client_observation?.now ?? at
  const video = camera === undefined ? drone.video : camera
  let status: MediaStreamStatus = video?.status ?? 'unreported'
  let reason = ''
  const lastFrameAt = video?.last_frame_at ?? null
  if (!observationCurrent(drone)) {
    status = drone.membership === 'disconnected' || drone.membership === 'leaving' ? 'offline' : 'unreported'
    reason = 'Current video unavailable. The device is offline or its relay observation is stale.'
  } else if (status === 'live' && (lastFrameAt === null || now < lastFrameAt || now - lastFrameAt > VIDEO_FRESH_MS)) {
    status = 'unreported'
    reason = 'Video report is stale or has no current frame timestamp. Current stream status is unknown.'
  }
  return {
    status,
    tone: STREAM_TONE[status],
    lastFrame: lastFrameAt === null ? 'no frame reported' : formatAge(now - lastFrameAt),
    degraded: status !== 'live',
    degradedWord: status === 'live' ? '' : reason || DEGRADED_WORD[status],
  }
}

export interface Word {
  text: string
  tone: Tone
}

export function deriveReadiness(drone: RelayAircraftState): Word {
  if (!observationCurrent(drone)) return { text: membershipWord(drone), tone: 'warn' }
  if (drone.client_observation && !motionObservationCurrent(drone) && drone.readiness_reasons.length === 0) return { text: 'Current motion telemetry unknown', tone: 'warn' }
  return drone.readiness_reasons.length > 0
    ? {
        text: drone.readiness_reasons.map((reason) => reason === 'control_authority_missing'
          ? 'Control not granted'
          : humanizeCode(reason)).join(', '),
        tone: 'danger',
      }
    : drone.pos_quality === 0
      ? { text: 'position quality 0%', tone: 'warn' }
      : { text: 'ready', tone: 'ok' }
}

/**
 * The newest capture_room request that targets this device, as the relay
 * has reported it so far. Nothing beyond the request lifecycle is known.
 */
export function deriveCaptureProgress(requests: RequestRecord[], droneId: DroneId): Word {
  const request = requests.find(
    (item) =>
      item.intent.name === 'capture_room' &&
      item.intent.selection.includes(droneId) &&
      item.status !== 'draft',
  )
  if (!request) return { text: 'none requested', tone: 'muted' }
  const text = request.status.replaceAll('_', ' ')
  switch (request.status) {
    case 'failed':
    case 'refused':
      return { text, tone: 'danger' }
    case 'invalidated':
    case 'cancelled':
      return { text, tone: 'warn' }
    case 'completed':
      return { text, tone: 'ok' }
    default:
      return { text, tone: 'ink' }
  }
}
