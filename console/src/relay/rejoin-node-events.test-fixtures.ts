/** Exact public node reports from the four-node browser demo's epoch-2 rejoin audit. */
const envelope = {
  v: 1, t: 1788660535448, session: 'fleet-browser-1788660521433', drone_id: 3, connection_epoch: 2,
} as const

export const REJOIN_NODE_EVENTS = [
  {
    ...envelope, type: 'capabilities', event_id: '8410f171-2331-450b-b6e4-f5c840ccef0c',
    aircraft_firmware: 'fake', aircraft_model: 'fake-mini3', android_version: 'fake',
    gimbal_pitch_max_deg: 30, gimbal_pitch_min_deg: -90, horizontal_fov_deg: 66, measured_hfov_deg: null,
    media_retrieval: true, native_panorama_modes: ['pano_360'], phone_model: 'fake-node',
    photo_capture: true, rc_firmware: 'fake', sdk_version: 'fake', storage_remaining_bytes: 50000000,
  },
  {
    ...envelope, type: 'node_status', event_id: 'd43f7e02-6f00-4700-aee8-fa7c1d8d0a3b',
    authority_change_reason: null, control_authority: true, phone_battery_percent: 81,
    phone_thermal_state: 'none', video_publish_state: 'stopped', virtual_stick_enabled: false,
    watchdog_state: 'nominal',
  },
  {
    ...envelope, type: 'capture_readiness', event_id: '9eccb984-740e-4528-a258-d7f06ffd1db6',
    camera_ok: true, capture_id: null, clearance_ok: true, coverage_missing: [],
    guidance_mode: 'visual_advisory', image_quality_ok: true, motion_ok: true, next_heading_deg: null,
    pose_ok: true, pose_source: 'operator_approved', room_id: null, storage_ok: true, suggested_delta: null,
  },
] as const
