import type { DeviceNoun, RequestRecord } from '../../control/state'
import { pluralNoun } from '../../control/state'
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

export type WallSize = 4 | 6

/** The first `count` devices by id; missing slots stay empty, never padded from a fixture. */
export function mosaicSlots(
  devices: RelayAircraftState[],
  count: WallSize,
): Array<RelayAircraftState | null> {
  return Array.from({ length: count }, (_, index) => devices[index] ?? null)
}

export function mosaicNote(count: WallSize, reported: number, noun: DeviceNoun = 'aircraft'): string {
  const base = `${count} tiles. Focus follows the operator's selection and survives video loss on the focused ${noun}.`
  if (reported > count) {
    return `${base} The relay reports ${reported} ${pluralNoun(noun)}; the first ${count} by id are shown.`
  }
  if (reported < count) {
    return `${base} ${reported} of ${count} slots have a reported ${noun}.`
  }
  return base
}

/** The ground vehicle wall: one slot per configured ground path. */
export const GROUND_WALL_SIZE: WallSize = 4

export function groundNote(reported: number): string {
  const base = `${GROUND_WALL_SIZE} tiles, one per ground vehicle in unit order; streams are named ground{unit}.`
  if (reported > GROUND_WALL_SIZE) {
    return `${base} The relay reports ${reported} robots; the first ${GROUND_WALL_SIZE} by id are shown.`
  }
  if (reported < GROUND_WALL_SIZE) {
    return `${base} ${reported} of ${GROUND_WALL_SIZE} slots have a reported robot.`
  }
  return base
}
