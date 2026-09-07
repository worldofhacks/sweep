import { DEVICE_FRESH_MS, observationCurrent, motionObservationCurrent } from '../../control/observation'
import { telemetryGroup } from '../devices/telemetry'
import { isFreshScan } from '../../sensor/status'
import { formatDeviceId } from '../../control/state'
import type { DeviceClass, DroneId, RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import type { SensorSnapshot } from '../../sensor/store'
import type { Observation } from '../../relay/observation'
import type { Point } from './projection'

/** A device the map can place, with everything the draw pass needs. */
export interface MapDevice {
  droneId: DroneId
  label: string
  deviceClass: DeviceClass
  unit: number
  x: number
  y: number
  /** Degrees counter-clockwise from +x; null when nothing reports a heading. */
  headingDeg: number | null
  /** Whether the position came from the relay's telemetry projection, a scan pose, or a signed observation. */
  source: 'telemetry' | 'scan' | 'observation'
}

/**
 * The relay projects Appendix B telemetry into each device's state. Its keys
 * are x, y, z, vx, vy, vz; a heading rides in the optional `heading_deg` the
 * planner already reads, and a node that names it `yaw_deg` is accepted too.
 * Anything else is no pose rather than a guessed one.
 */
export function telemetryPose(
  telemetry: unknown,
): { x: number; y: number; headingDeg: number | null } | null {
  if (typeof telemetry !== 'object' || telemetry === null || Array.isArray(telemetry)) return null
  const record = telemetry as Record<string, unknown>
  const x = finite(record.x)
  const y = finite(record.y)
  if (x === null || y === null) return null
  return { x, y, headingDeg: heading(record.heading_deg) ?? heading(record.yaw_deg) }
}

/**
 * Where each device is: its telemetry projection, else the pose of its newest
 * scan from this connection epoch. A device neither reports is not placed.
 */
export function mapDevices(
  fleet: readonly RelayAircraftState[],
  sensors: SensorSnapshot,
  now?: number,
): MapDevice[] {
  const devices: MapDevice[] = []
  for (const device of fleet) {
    if (!observationCurrent(device)) continue
    const latest = deviceScan(device, sensors)
    const scan = latest && (now === undefined || isFreshScan(latest, now)) ? latest : null
    const pose = device.client_observation && !motionObservationCurrent(device) ? null : telemetryPose(device.telemetry)
    const common = {
      droneId: device.drone_id,
      label: formatDeviceId(device),
      deviceClass: device.device_class,
      unit: device.unit,
    }
    if (pose) {
      devices.push({
        ...common,
        x: pose.x,
        y: pose.y,
        headingDeg: pose.headingDeg ?? scan?.pose.yaw_deg ?? null,
        source: 'telemetry',
      })
    } else if (scan) {
      devices.push({
        ...common,
        x: scan.pose.x,
        y: scan.pose.y,
        headingDeg: scan.pose.yaw_deg,
        source: 'scan',
      })
    }
  }
  return devices
}

/** Only registered world poses enter the shared map. Source-local odom stays local to its producer. */
export function canonicalMapDevices(
  fleet: readonly RelayAircraftState[],
  observations: readonly Observation[],
  now: number,
): MapDevice[] {
  const byId = new Map(fleet.map((device) => [device.drone_id, device]))
  const newest = new Map<number, Observation>()
  for (const observation of observations) {
    if (observation.payload.kind !== 'pose' || observation.payload.pose.parent_frame !== 'world') continue
    const device = byId.get(observation.device_id)
    if (
      !device ||
      device.node_type !== 'ground' ||
      device.connection_epoch !== observation.connection_epoch ||
      observation.node_type !== 'ground' ||
      observation.confidence <= 0 ||
      observation.t_ingest > now ||
      now - observation.t_ingest > DEVICE_FRESH_MS ||
      (device.ground_readiness?.source_id !== null && device.ground_readiness?.source_id !== undefined &&
        device.ground_readiness.source_id !== observation.source_id)
    ) continue
    const prior = newest.get(device.drone_id)
    if (!prior || observation.t_ingest > prior.t_ingest ||
      (observation.t_ingest === prior.t_ingest && observation.event_id > prior.event_id)) {
      newest.set(device.drone_id, observation)
    }
  }
  const devices: MapDevice[] = []
  for (const [droneId, observation] of newest) {
    const device = byId.get(droneId)
    if (!device || observation.payload.kind !== 'pose') continue
    devices.push({
      droneId: device.drone_id,
      label: formatDeviceId(device),
      deviceClass: device.device_class,
      unit: device.unit,
      x: observation.payload.pose.x_m,
      y: observation.payload.pose.y_m,
      headingDeg: null,
      source: 'observation',
    })
  }
  return devices
}

/** The newest scan per device, dropped when it predates the device's current epoch. */
export function scanningDevices(
  fleet: readonly RelayAircraftState[],
  sensors: SensorSnapshot,
): Array<{ device: RelayAircraftState; scan: RelaySensorEvent }> {
  const pairs: Array<{ device: RelayAircraftState; scan: RelaySensorEvent }> = []
  for (const device of fleet) {
    if (!observationCurrent(device)) continue
    const scan = deviceScan(device, sensors)
    if (scan) pairs.push({ device, scan })
  }
  return pairs
}

/** The scan poses held in the store's ring, oldest first: the device's short trail. */
export function scanTrail(device: RelayAircraftState, sensors: SensorSnapshot): Point[] {
  if (telemetryGroup(device, 'lidar')?.calibrated === false) return []
  const trail = sensors.trails[device.drone_id] ?? []
  return trail
    .filter((scan) => scan.connection_epoch === device.connection_epoch)
    .map((scan) => ({ x: scan.pose.x, y: scan.pose.y }))
}

/**
 * The device's newest scan, or null when the store holds none or holds one
 * from an earlier connection epoch: that scan says nothing about where the
 * device is in this one.
 */
export function deviceScan(
  device: Pick<RelayAircraftState, 'drone_id' | 'connection_epoch' | 'node_status'>,
  sensors: SensorSnapshot,
): RelaySensorEvent | null {
  const lidar = device.node_status?.device_telemetry?.lidar
  if (lidar && typeof lidar === 'object' && !Array.isArray(lidar) && lidar.calibrated === false) return null
  const scan = sensors.latest[device.drone_id]
  if (!scan || scan.connection_epoch !== device.connection_epoch) return null
  return scan
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function heading(value: unknown): number | null {
  const parsed = finite(value)
  if (parsed === null) return null
  const wrapped = parsed % 360
  return wrapped < 0 ? wrapped + 360 : wrapped
}
