import type { RequestRecord } from '../../control/state'
import type { DroneId, MediaStreamStatus, RelayAircraftState } from '../../relay/contract'
import type { Tone } from '../../shell/derive'

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

export function deriveStream(drone: RelayAircraftState, now: number): StreamView {
  const video = drone.video
  const status: MediaStreamStatus = video?.status ?? 'unreported'
  const lastFrameAt = video?.last_frame_at ?? null
  return {
    status,
    tone: STREAM_TONE[status],
    lastFrame: lastFrameAt === null ? 'no frame reported' : formatAge(now - lastFrameAt),
    degraded: status !== 'live',
    degradedWord: status === 'live' ? '' : DEGRADED_WORD[status],
  }
}

export interface Word {
  text: string
  tone: Tone
}

export function deriveReadiness(drone: RelayAircraftState): Word {
  return drone.readiness_reasons.length > 0
    ? { text: drone.readiness_reasons.join(', '), tone: 'danger' }
    : { text: 'ready', tone: 'ok' }
}

/**
 * The newest capture_room request that targets this aircraft, as the relay
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

export type WallSize = 4 | 6

/** The first `count` aircraft by id; missing slots stay empty, never padded from a fixture. */
export function mosaicSlots(
  aircraft: RelayAircraftState[],
  count: WallSize,
): Array<RelayAircraftState | null> {
  return Array.from({ length: count }, (_, index) => aircraft[index] ?? null)
}

export function mosaicNote(count: WallSize, reported: number): string {
  const base = `${count} tiles. Focus follows the operator's selection and survives video loss on the focused aircraft.`
  if (reported > count) {
    return `${base} The relay reports ${reported} aircraft; the first ${count} by id are shown.`
  }
  if (reported < count) {
    return `${base} ${reported} of ${count} slots have a reported aircraft.`
  }
  return base
}
