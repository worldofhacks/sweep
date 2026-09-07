import { describe, expect, test } from 'vitest'
import { parseObservation } from './observation'

function telemetry(frame = 'world') {
  return {
    kind: 'telemetry',
    position: { frame, x_m: 1, y_m: 2, z_m: 1.5 },
    velocity: { frame, x_m_s: 0, y_m_s: 0, z_m_s: 0 },
    battery: 0.8,
    link: 0.9,
    pos_quality: 0.7,
    state: 'hovering',
  }
}

function observation(payload: Record<string, unknown> = telemetry()) {
  return {
    v: 1,
    type: 'observation',
    event_id: 'observation-001',
    session: 'demo-1',
    device_id: 7,
    connection_epoch: 3,
    source_id: 'bridge',
    node_type: 'aircraft',
    frame: 'world',
    confidence: 0.9,
    t_capture: { clock_id: 'bridge-ms', unit: 'ms', value: 100 },
    t_source_receipt: { clock_id: 'bridge-ms', unit: 'ms', value: 101 },
    clock_mapping_id: 'bridge-clock',
    payload,
    t_ingest: 1_005,
  }
}

describe('observation v1 console mirror', () => {
  test('parses the aircraft golden shape into an immutable observation', () => {
    const parsed = parseObservation(observation())
    expect(parsed?.payload.kind).toBe('telemetry')
    expect(Object.isFrozen(parsed)).toBe(true)
    expect(Object.isFrozen(parsed?.payload)).toBe(true)
  })

  test('parses local lidar and camera-tag evidence without a world claim', () => {
    const scan: Record<string, unknown> = observation({
      kind: 'range_scan',
      sensor_pose: { parent_frame: 'odom', child_frame: 'lidar', x_m: 0, y_m: 0, z_m: 0.25, qx: 0, qy: 0, qz: 0, qw: 1 },
      angle_min_rad: -0.1,
      angle_increment_rad: 0.1,
      range_min_m: 0.1,
      range_max_m: 8,
      ranges_m: [1, null, 2],
      mount_id: 'ohmni-lidar-v1',
    })
    scan.node_type = 'ground'
    scan.frame = 'lidar'
    scan.t_capture = null
    scan.clock_mapping_id = null
    expect(parseObservation(scan)?.payload.kind).toBe('range_scan')

    const tag: Record<string, unknown> = observation({
      kind: 'tag_observation', family: 'tag36h11', tag_id: 42, image_id: 'frame-001', pose_accepted: true,
      tag_pose: { parent_frame: 'camera', child_frame: 'tag:42', x_m: 0.2, y_m: 0, z_m: 1, qx: 0, qy: 0, qz: 0, qw: 1 },
      covariance_m2: [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.02], reason: 'pose', size_m: 0.16,
      corners_px: [[100, 200], [120, 200], [120, 220], [100, 220]], pixel_frame: 'camera', reprojection_rms_px: 0.3,
    })
    tag.node_type = 'ground'
    tag.frame = 'camera'
    tag.t_capture = null
    tag.clock_mapping_id = null
    expect(parseObservation(tag)?.payload.kind).toBe('tag_observation')
  })

  test.each([
    ['node_type', (value: Record<string, unknown>) => { value.node_type = [] }],
    ['frame', (value: Record<string, unknown>) => { value.frame = [] }],
    ['payload kind', (value: Record<string, unknown>) => { (value.payload as Record<string, unknown>).kind = {} }],
    ['source clock unit', (value: Record<string, unknown>) => { ((value.t_source_receipt as Record<string, unknown>).unit) = [] }],
    ['tag reason', (value: Record<string, unknown>) => { value.payload = { kind: 'tag_observation', family: 'tag36h11', tag_id: 42, image_id: 'frame-001', pose_accepted: false, tag_pose: null, covariance_m2: null, reason: [], size_m: null, corners_px: [[100, 200], [120, 200], [120, 220], [100, 220]], pixel_frame: 'camera', reprojection_rms_px: null } }],
  ])('rejects malformed %s containers', (_, mutate) => {
    const value = observation()
    mutate(value)
    expect(parseObservation(value)).toBeNull()
  })
})
