import type {
  ConnectionStatus,
  ControlState,
  OperatorNotice,
  RequestRecord,
} from '../control/state'
import { formatDroneId } from '../control/state'
import type { DroneId, RelayAircraftState } from '../relay/contract'
import { formatTime } from './format'

export type Tone = 'ok' | 'warn' | 'danger' | 'muted' | 'ink'

/** The design treats a degraded socket as connected: frames still flow. */
export function isLinkUp(status: ConnectionStatus): boolean {
  return status === 'connected' || status === 'degraded'
}

export function connectionTone(status: ConnectionStatus): Tone {
  if (status === 'connected') return 'ok'
  if (status === 'degraded') return 'warn'
  if (status === 'disconnected') return 'danger'
  return 'muted'
}

export interface StopTimes {
  /** When this console first saw the relay report estop true; null when never seen. */
  seenActiveAt: number | null
  /** When this console saw the relay report estop false after it was true. */
  seenClearedAt: number | null
}

export const STOP_CLEARED_NOTICE_MS = 10_000

export interface StopView {
  title: string
  sub: string
  active: boolean
  disabled: boolean
  reason: string
}

export function deriveStop(state: ControlState, times: StopTimes, now: number): StopView {
  const up = isLinkUp(state.connection.status)
  const active = state.estop
  const title = active ? 'Stop active' : 'Network stop'
  const sub = active
    ? times.seenActiveAt === null
      ? 'active · Shift+Escape'
      : `seen ${formatTime(times.seenActiveAt)} · Shift+Escape`
    : 'estop · Shift+Escape'
  let reason: string
  if (!up) {
    const relayReason = state.connection.reason ? ` ${state.connection.reason}` : ''
    reason = `Disabled: the console socket is ${state.connection.status}.${relayReason} Use the physical RC or Shift+Escape on the keyboard connection.`
  } else if (active) {
    reason =
      'Pressing again re-sends estop. Nothing in the console clears a stop; it clears when the relay reports estop false.'
  } else if (
    times.seenClearedAt !== null &&
    now - times.seenClearedAt < STOP_CLEARED_NOTICE_MS
  ) {
    reason = `Stop cleared, seen ${formatTime(times.seenClearedAt)}, reported by the relay.`
  } else {
    reason = 'Sends estop to every aircraft in the roster.'
  }
  return { title, sub, active, disabled: !up, reason }
}

export interface StateTag {
  id: 'armed' | 'stop' | 'mode'
  label: string
  variant: 'armed' | 'disarmed' | 'stop-active' | 'stop-clear' | 'mode'
}

export function deriveStateTags(state: ControlState): StateTag[] {
  return [
    {
      id: 'armed',
      label: state.armed ? 'Armed' : 'Disarmed',
      variant: state.armed ? 'armed' : 'disarmed',
    },
    {
      id: 'stop',
      label: state.estop ? 'Stop active' : 'Stop clear',
      variant: state.estop ? 'stop-active' : 'stop-clear',
    },
    { id: 'mode', label: 'indoor', variant: 'mode' },
  ]
}

export function sortedAircraft(aircraft: ControlState['aircraft']): RelayAircraftState[] {
  return Object.values(aircraft).sort((a, b) => a.drone_id - b.drone_id)
}

export function isReady(drone: RelayAircraftState | undefined): boolean {
  return Boolean(drone && drone.membership === 'ready' && drone.selectable)
}

export function deriveSelectionLabel(selection: DroneId[]): string {
  return selection.length ? selection.map(formatDroneId).join('  ') : 'none selected'
}

export function deriveReadyCount(aircraft: ControlState['aircraft']): string {
  const fleet = sortedAircraft(aircraft)
  const ready = fleet.filter(isReady).length
  return `${ready} of ${fleet.length} ready`
}

export interface RcLine {
  text: string
  danger: boolean
}

export function deriveRcLine(state: ControlState): RcLine {
  const fleet = sortedAircraft(state.aircraft)
  const ids = state.selection.length ? state.selection : fleet.slice(0, 1).map((d) => d.drone_id)
  if (ids.length === 0) return { text: 'no aircraft reported', danger: false }
  const text = ids
    .map((id) => {
      const drone = state.aircraft[id]
      if (!drone) return `${formatDroneId(id)} unreported`
      const authority = drone.control_authority ? 'Sweep' : 'RC takeover'
      const rc = drone.rc_safety_operator_present ? 'present' : 'absent'
      return `${formatDroneId(id)} ${authority} · RC operator ${rc}`
    })
    .join('   ')
  const danger = state.selection.some((id) => {
    const drone = state.aircraft[id]
    return !drone || !drone.control_authority || !drone.rc_safety_operator_present
  })
  return { text, danger }
}

export interface LinkPill {
  id: 'relay' | 'keys' | 'webcam'
  label: string
  short: string
  value: string
  tone: Tone
}

export function deriveLinks(state: ControlState, webcam?: ConnectionStatus): LinkPill[] {
  const links: LinkPill[] = [
    {
      id: 'relay',
      label: 'Relay (console)',
      short: 'relay',
      value: state.connection.status,
      tone: connectionTone(state.connection.status),
    },
    {
      id: 'keys',
      label: 'Keyboard stop',
      short: 'keys',
      value: state.keyboardConnection.status,
      tone: connectionTone(state.keyboardConnection.status),
    },
  ]
  if (webcam !== undefined) {
    links.push({
      id: 'webcam',
      label: 'Webcam',
      short: 'webcam',
      value: webcam,
      tone: connectionTone(webcam),
    })
  }
  return links
}

export function newestDanger(notices: OperatorNotice[]): OperatorNotice | null {
  return notices.find((notice) => notice.level === 'danger') ?? null
}

export function noticeSummary(notices: OperatorNotice[]): string {
  const count = (level: OperatorNotice['level']) =>
    notices.filter((notice) => notice.level === level).length
  return `${count('danger')} danger · ${count('warning')} warning · ${count('info')} info`
}

export interface InvalidationView {
  reasonCode: string
  detail: string
}

/**
 * The newest request was invalidated before it was ever sent and nothing else is
 * pending: the footer says so until the next draft or send replaces it.
 */
export function deriveInvalidation(
  requests: RequestRecord[],
  pending: RequestRecord | null,
): InvalidationView | null {
  if (pending) return null
  const newest = requests[0]
  if (!newest || newest.status !== 'invalidated' || newest.timestamps.sent !== undefined) return null
  return {
    reasonCode: newest.reasonCode ?? 'invalidated',
    detail: newest.detail ?? 'The relay invalidated this plan.',
  }
}

export function membershipTone(membership: RelayAircraftState['membership']): Tone {
  if (membership === 'ready') return 'ok'
  if (membership === 'degraded' || membership === 'leaving') return 'warn'
  if (membership === 'disconnected') return 'danger'
  return 'ink'
}

export function metricTone(value: number | null): Tone {
  if (value === null) return 'muted'
  const percent = value * 100
  if (percent < 25) return 'danger'
  if (percent < 55) return 'warn'
  return 'ink'
}
