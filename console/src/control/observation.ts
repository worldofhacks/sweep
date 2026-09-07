import type { RelayAircraftState } from '../relay/contract'
import type { Observation } from '../relay/observation'
import type { ControlState } from './state'

export const DEVICE_FRESH_MS = 5_000
export type DeviceObservation = {
  state: 'current' | 'offline' | 'stale' | 'unknown'
  reason: string
  /** Relay-clock estimate anchored to an accepted snapshot's local receipt. */
  now: number
  ground?: GroundObservation
}

export type GroundObservation = {
  lastReportAt: number | null
  pose: Observation | null
  telemetry: Observation | null
  poseCurrent: boolean
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
  if (!observationCurrent(device)) return false
  if (device?.node_type === 'ground') return device.client_observation?.ground?.poseCurrent === true
  if (!device?.telemetry || device.telemetry.fresh === false) return false
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
  return { ...state, aircraft: Object.fromEntries(Object.entries(state.aircraft).map(([id, device]) => {
    const ground = groundObservation(device, Object.values(state.latestObservations), now)
    const observation = ground && ground.lastReportAt !== null
      ? observeGroundDevice(device, now, connected, ground)
      : observeDevice(device, now, connected)
    return [id, { ...device, client_observation: observation }]
  })) }
}

function observeGroundDevice(
  device: RelayAircraftState,
  now: number,
  connected: boolean,
  ground: GroundObservation,
): DeviceObservation {
  if (device.membership === 'disconnected' || device.membership === 'leaving') {
    return { state: 'offline', reason: 'The relay reports this device disconnected.', now, ground }
  }
  if (!connected) return { state: 'unknown', reason: 'Relay connection unavailable; current device state is unknown.', now, ground }
  if (ground.lastReportAt === null || now < ground.lastReportAt) {
    return { state: 'unknown', reason: 'No current accepted ground observation is available.', now, ground }
  }
  if (now - ground.lastReportAt > DEVICE_FRESH_MS) {
    return { state: 'stale', reason: 'No fresh accepted ground observation for over 5 seconds; current state is unknown.', now, ground }
  }
  return { state: 'current', reason: 'Fresh accepted ground observation.', now, ground }
}

function groundObservation(
  device: RelayAircraftState,
  observations: readonly Observation[],
  now: number,
): GroundObservation | undefined {
  if (device.node_type !== 'ground') return undefined
  const current = observations.filter((observation) => observation.device_id === device.drone_id &&
    observation.connection_epoch === device.connection_epoch && observation.node_type === 'ground')
  const latest = current.reduce<Observation | null>((newest, observation) =>
    newest === null || observation.t_ingest > newest.t_ingest ? observation : newest, null)
  const newestOf = (kind: Observation['payload']['kind'], sourceId?: string | null) => current.reduce<Observation | null>((newest, observation) =>
    observation.payload.kind !== kind || (sourceId !== undefined && observation.source_id !== sourceId) ||
      (newest !== null && newest.t_ingest >= observation.t_ingest)
      ? newest
      : observation, null)
  const pose = newestOf('pose', device.ground_readiness?.source_id ?? null)
  const telemetry = newestOf('telemetry')
  return {
    lastReportAt: latest?.t_ingest ?? null,
    pose,
    telemetry,
    poseCurrent: pose !== null && pose.confidence > 0 && now >= pose.t_ingest && now - pose.t_ingest <= DEVICE_FRESH_MS,
  }
}
