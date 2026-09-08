import { publicNodeEvents } from '../../testing/public-node-events'
import { describe, expect, test } from 'vitest'
import type { RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import type { SensorSnapshot } from '../../sensor/store'
import { scanningDevices, mapDevices, scanTrail, telemetryPose } from './derive-map'

function device(overrides: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return {
    drone_id: 11,
    device_class: 'ground_vehicle',
    unit: 1,
    connection_epoch: 3,
    membership: 'ready',
    readiness_reasons: [],
    flight_state: 'idle',
    battery: 0.7,
    link: 0.8,
    pos_quality: 0.6,
    control_authority: true,
    rc_safety_operator_present: true,
    last_seen_at: 1_756_700_000_000,
    camera_patterns: [],
    selectable: true,
    adapter_id: 'ohmni-01',
    adapter_capabilities: ['class:ground_vehicle', 'ground_drive', 'lidar'],
    home_pose: null,
    telemetry: null,
    membership_history: [],
    membership_history_truncated: 0,
    ...overrides,
  }
}

function scan(overrides: Partial<RelaySensorEvent> = {}): RelaySensorEvent {
  return {
    v: 1,
    t: 1_756_700_000_000,
    type: 'sensor',
    event_id: 'scan-1',
    session: 'derive-map-test',
    drone_id: 11,
    connection_epoch: 3,
    kind: 'lidar_scan',
    pose: { x: 2, y: -1, yaw_deg: 45 },
    angle_min_deg: 0,
    angle_increment_deg: 1,
    range_min_m: 0.15,
    range_max_m: 12,
    ranges_cm: Array.from({ length: 360 }, () => 0),
    ...overrides,
  }
}

function snapshot(scans: RelaySensorEvent[]): SensorSnapshot {
  const latest: Record<number, RelaySensorEvent> = {}
  const trails: Record<number, RelaySensorEvent[]> = {}
  for (const item of scans) {
    latest[item.drone_id] = item
    trails[item.drone_id] = [...(trails[item.drone_id] ?? []), item]
  }
  return { latest, trails }
}

describe('map derivation', () => {
  test('a telemetry projection places a device; a heading rides in heading_deg or yaw_deg', () => {
    expect(telemetryPose({ x: 1.5, y: -2, z: 0, vx: 0, vy: 0, vz: 0 })).toEqual({
      x: 1.5,
      y: -2,
      headingDeg: null,
    })
    expect(telemetryPose({ x: 0, y: 0, heading_deg: 30, yaw_deg: 200 })?.headingDeg).toBe(30)
    expect(telemetryPose({ x: 0, y: 0, yaw_deg: 200 })?.headingDeg).toBe(200)
    expect(telemetryPose({ x: 0, y: 0, yaw_deg: -90 })?.headingDeg).toBe(270)
  })

  test('anything that is not a pose is no pose', () => {
    expect(telemetryPose(null)).toBeNull()
    expect(telemetryPose({ fresh: true })).toBeNull()
    expect(telemetryPose({ x: 1 })).toBeNull()
    expect(telemetryPose({ x: 1, y: Number.NaN })).toBeNull()
    expect(telemetryPose([1, 2])).toBeNull()
    expect(telemetryPose({ x: '1', y: '2' })).toBeNull()
  })

  test('telemetry places the device; the newest scan supplies a heading telemetry lacks', () => {
    const placed = mapDevices([device({ telemetry: { x: 1, y: 2 } })], snapshot([scan()]))
    expect(placed).toEqual([
      {
        droneId: 11,
        label: 'G-01',
        deviceClass: 'ground_vehicle',
        unit: 1,
        x: 1,
        y: 2,
        headingDeg: 45,
        source: 'telemetry',
      },
    ])
  })

  test('without telemetry the scan pose places the device, and without either it is not placed', () => {
    const fromScan = mapDevices([device()], snapshot([scan()]))
    expect(fromScan).toMatchObject([{ x: 2, y: -1, headingDeg: 45, source: 'scan' }])
    expect(mapDevices([device()], snapshot([]))).toEqual([])
  })

  test('a scan from an earlier connection epoch is not this session of the device', () => {
    const stale = snapshot([scan({ connection_epoch: 2 })])
    expect(mapDevices([device()], stale)).toEqual([])
    expect(scanningDevices([device()], stale)).toEqual([])
    expect(scanTrail(device(), stale)).toEqual([])
    expect(scanningDevices([device()], snapshot([scan()]))).toHaveLength(1)
  })

  test('explicitly uncalibrated sensor data never supplies world-map points or heading', () => {
    const [, status] = publicNodeEvents('derive-map-test', 3)
    const raw = device({ node_status: { ...status, drone_id: 11, device_telemetry: { lidar: { calibrated: false, ranges_cm: [10, 20] } } } })
    const scans = snapshot([scan()])
    expect(scanningDevices([raw], scans)).toEqual([])
    expect(scanTrail(raw, scans)).toEqual([])
    expect(mapDevices([raw], scans)).toEqual([])
    expect(mapDevices([{ ...raw, telemetry: { x: 1, y: 2 } }], scans)).toMatchObject([{ x: 1, y: 2, headingDeg: null }])
  })

  test('the trail is the ring of scan poses, oldest first', () => {
    const ring = snapshot([
      scan({ event_id: 'a', pose: { x: 0, y: 0, yaw_deg: 0 } }),
      scan({ event_id: 'b', pose: { x: 1, y: 0, yaw_deg: 0 } }),
      scan({ event_id: 'c', connection_epoch: 2, pose: { x: 9, y: 9, yaw_deg: 0 } }),
    ])
    expect(scanTrail(device(), ring)).toEqual([
      { x: 0, y: 0 },
      { x: 1, y: 0 },
    ])
  })
})
