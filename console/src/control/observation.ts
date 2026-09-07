import type { RelayAircraftState } from '../relay/contract'
import type { ControlState } from './state'

export const DEVICE_FRESH_MS = 5_000
export type DeviceObservation = {
  state: 'current' | 'offline' | 'stale' | 'unknown'
  reason: string
  /** Relay-clock estimate anchored to an accepted snapshot's local receipt. */
  now: number
}

/** Browser freshness never rewrites a relay membership, motion, or authority fact. */
export function observeDevice(device: RelayAircraftState, now: number, connected = true): DeviceObservation {
  if (device.membership === 'disconnected' || device.membership === 'leaving') return { state: 'offline', reason: 'The relay reports this device disconnected.', now }
  if (!connected) return { state: 'unknown', reason: 'Relay connection unavailable; current device state is unknown.', now }
  if (device.last_seen_at === null || now < device.last_seen_at) return { state: 'unknown', reason: 'No current device timestamp is available.', now }
  if (now - device.last_seen_at > DEVICE_FRESH_MS) return { state: 'stale', reason: 'No fresh device report for over 5 seconds; current state is unknown.', now }
  return { state: 'current', reason: 'Fresh device report.', now }
}

export function observationCurrent(device: RelayAircraftState | undefined): boolean {
  return Boolean(device && !['disconnected', 'leaving'].includes(device.membership) &&
    (!device.client_observation || device.client_observation.state === 'current'))
}

/** A connected bridge does not make an old flight/drive report current. */
export function motionObservationCurrent(device: RelayAircraftState | undefined): boolean {
  if (!observationCurrent(device) || !device?.telemetry || device.telemetry.fresh === false) return false
  const reportedAt = device.telemetry.t
  if (reportedAt === undefined) return device.telemetry.fresh === true
  const now = device.client_observation?.now ?? device.last_seen_at
  return now !== null && now >= reportedAt && now - reportedAt <= DEVICE_FRESH_MS
}

export function membershipWord(device: RelayAircraftState): string {
  if (device.membership === 'disconnected' || device.membership === 'leaving') return 'offline'
  const status = device.client_observation?.state
  return status && status !== 'current' ? (status === 'stale' ? 'stale · current state unknown' : status) : device.membership
}

export function observedControlState(state: ControlState, localNow: number): ControlState {
  const last = state.lastStateEvent
  const now = last?.receivedAt === undefined ? localNow : last.t + Math.max(0, localNow - last.receivedAt)
  const connected = ['connected', 'degraded'].includes(state.connection.status)
  return { ...state, aircraft: Object.fromEntries(Object.entries(state.aircraft).map(([id, device]) =>
    [id, { ...device, client_observation: observeDevice(device, now, connected) }])) }
}
