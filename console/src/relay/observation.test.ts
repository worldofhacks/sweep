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

function captureAlignment() {
  return {
    v: 1,
    alignment_config_id: 'ohmni-alignment-fixture',
    alignment_config_sha256: '0877d81fe8e9cd96e07ca6fd0374d8dd2b3fba072d96d6a2dff799a1bbe652d0',
    kinematic_calibration_id: 'ohmni-mount-fixture',
    kinematic_calibration_sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    frame_pts: { clock_id: 'dji_stream_presentation_ms', unit: 'ms', value: 1000 },
    gimbal_receipt: { clock_id: 'phone_elapsed_realtime_ms', unit: 'ms', value: 1001010 },
    body_attitude_receipt: { clock_id: 'phone_elapsed_realtime_ms', unit: 'ms', value: 1001008 },
    gimbal_attitude: { yaw_deg: 90, pitch_deg: 0, roll_deg: 0 },
    body_attitude: { yaw_deg: 1, pitch_deg: 0, roll_deg: 0 },
    frame_capture_error_ms: 5,
    gimbal_callback_latency_ms: 25,
    body_attitude_callback_latency_ms: 25,
    gimbal_callback_orientation_error_deg: 0.1,
    body_attitude_callback_orientation_error_deg: 0.1,
    gimbal_angular_rate_bound_deg_s: 10,
    body_angular_rate_bound_deg_s: 10,
    max_extrinsics_angle_error_deg: 1,
  }
}

describe('observation v1 console mirror', () => {
  test('parses the aircraft golden shape into an immutable observation', () => {
    const parsed = parseObservation(observation())
    expect(parsed?.payload.kind).toBe('telemetry')
    expect(Object.isFrozen(parsed)).toBe(true)
    expect(Object.isFrozen(parsed?.payload)).toBe(true)
  })

  test('parses the canonical phone camera-alignment payload', () => {
    const value = observation({
      kind: 'pose',
      pose: {
        parent_frame: 'body', child_frame: 'camera',
        x_m: 0, y_m: 0, z_m: 0, qx: 0, qy: 0, qz: Math.SQRT1_2, qw: Math.SQRT1_2,
      },
      capture_alignment: captureAlignment(),
    })
    value.source_id = 'dji-body-camera'
    value.frame = 'body'
    value.t_capture = { clock_id: 'phone_elapsed_realtime_ms', unit: 'ms', value: 1001000 }
    value.t_source_receipt = { clock_id: 'phone_elapsed_realtime_ms', unit: 'ms', value: 1001020 }
    value.clock_mapping_id = 'dji-pts-phone-fixture'

    const parsed = parseObservation(value)
    expect(parsed?.payload.kind).toBe('pose')
    if (parsed?.payload.kind === 'pose') {
      expect(parsed.payload.capture_alignment?.gimbal_attitude.yaw_deg).toBe(90)
    }
  })

  test('rejects a malformed capture-alignment artifact digest', () => {
    const value = observation({
      kind: 'pose',
      pose: {
        parent_frame: 'body', child_frame: 'camera',
        x_m: 0, y_m: 0, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1,
      },
      capture_alignment: { ...captureAlignment(), alignment_config_sha256: 'not-a-digest' },
    })
    value.frame = 'body'

    expect(parseObservation(value)).toBeNull()
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
    ['capture time', (value: Record<string, unknown>) => { value.t_capture = {} }],
    ['tag reason', (value: Record<string, unknown>) => { value.payload = { kind: 'tag_observation', family: 'tag36h11', tag_id: 42, image_id: 'frame-001', pose_accepted: false, tag_pose: null, covariance_m2: null, reason: [], size_m: null, corners_px: [[100, 200], [120, 200], [120, 220], [100, 220]], pixel_frame: 'camera', reprojection_rms_px: null } }],
  ])('rejects malformed %s containers', (_, mutate) => {
    const value = observation()
    mutate(value)
    expect(parseObservation(value)).toBeNull()
  })

  test('does not treat malformed nullable tag fields as absent', () => {
    const tag = observation({
      kind: 'tag_observation', family: 'tag36h11', tag_id: 42, image_id: 'frame-001', pose_accepted: false,
      tag_pose: {}, covariance_m2: [], reason: 'ambiguous', size_m: {},
      corners_px: [[100, 200], [120, 200], [120, 220], [100, 220]], pixel_frame: 'camera', reprojection_rms_px: null,
    })
    expect(parseObservation(tag)).toBeNull()
  })
})
