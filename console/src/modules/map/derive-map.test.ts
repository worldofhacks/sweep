import { publicNodeEvents } from '../../testing/public-node-events'
import { describe, expect, test } from 'vitest'
import type { RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import type { Observation } from '../../relay/observation'
import type { SensorSnapshot } from '../../sensor/store'
import { canonicalMapDevices, scanningDevices, mapDevices, scanTrail, telemetryPose } from './derive-map'

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
  test('renders a ground node from a signed world pose and excludes source-local scans', () => {
    const worldPose = {
      v: 1 as const, type: 'observation' as const, event_id: 'ground-world-pose', session: 'derive-map-test', device_id: 11,
      connection_epoch: 3, source_id: 'ohmni-pose', node_type: 'ground' as const, frame: 'world', confidence: 0.9,
      t_capture: null, t_source_receipt: { clock_id: 'ohmni-ms', unit: 'ms' as const, value: 100 }, clock_mapping_id: null,
      payload: { kind: 'pose' as const, pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 2, y_m: -1, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } }, t_ingest: 101,
    }
    const localScan = { ...worldPose, event_id: 'ground-local-scan', frame: 'lidar', payload: { kind: 'range_scan' as const, sensor_pose: { parent_frame: 'odom', child_frame: 'lidar', x_m: 0, y_m: 0, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 }, angle_min_rad: 0, angle_increment_rad: 0.1, range_min_m: 0.1, range_max_m: 8, ranges_m: [1], mount_id: 'lidar' } }
    expect(canonicalMapDevices([device({ node_type: 'ground' })], [worldPose, localScan], 102)).toMatchObject([
      { label: 'G-01', x: 2, y: -1, source: 'observation' },
    ])
  })

  test('renders only the newest fresh, accepted world pose from a ground source of record', () => {
    const worldPose = (overrides: Partial<Observation> = {}): Observation => ({
      v: 1 as const, type: 'observation' as const, event_id: 'ground-pose', session: 'derive-map-test', device_id: 11,
      connection_epoch: 3, source_id: 'ohmni-pose', node_type: 'ground' as const, frame: 'world', confidence: 0.9,
      t_capture: null, t_source_receipt: { clock_id: 'ohmni-ms', unit: 'ms' as const, value: 200 }, clock_mapping_id: null,
      payload: { kind: 'pose' as const, pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 2, y_m: -1, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } }, t_ingest: 200,
      ...overrides,
    })
    const sourceOfRecord = device({ node_type: 'ground', ground_readiness: { source_id: 'ohmni-pose' } })
    const wrongSource = worldPose({ event_id: 'other-source', source_id: 'other-pose', t_ingest: 202, payload: { kind: 'pose', pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 99, y_m: 99, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } } })
    const newest = worldPose({ event_id: 'newest', t_ingest: 201, payload: { kind: 'pose', pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 5, y_m: 6, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } } })

    const authoritative = canonicalMapDevices([sourceOfRecord], [worldPose(), wrongSource, newest], 202)
    expect(authoritative).toHaveLength(1)
    expect(authoritative).toMatchObject([{ x: 5, y: 6 }])
    const unconstrained = canonicalMapDevices([device({ node_type: 'ground' })], [worldPose(), wrongSource, newest], 202)
    expect(unconstrained).toHaveLength(1)
    expect(unconstrained).toMatchObject([{ x: 99, y: 99 }])
    expect(canonicalMapDevices([sourceOfRecord], [
      worldPose({ event_id: 'no-confidence', confidence: 0 }),
      worldPose({ event_id: 'stale', t_ingest: 202 - 5_001 }),
      worldPose({ event_id: 'wrong-epoch', connection_epoch: 2 }),
      worldPose({ event_id: 'wrong-node', node_type: 'aircraft' }),
      worldPose({ event_id: 'wrong-device', device_id: 12 }),
    ], 202)).toEqual([])
  })
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
