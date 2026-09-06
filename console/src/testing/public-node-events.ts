import type { RelayCapabilitiesEvent, RelayCaptureReadinessEvent, RelayNodeStatusEvent } from '../relay/contract'

/** Public phone audit shapes; credentials and device identifiers are not fixtures. */
export function publicNodeEvents(session = 'node-event-test', connectionEpoch = 5): [RelayCapabilitiesEvent, RelayNodeStatusEvent, RelayCaptureReadinessEvent] {
  const envelope = { v: 1 as const, t: 1788726306375, session, drone_id: 2, connection_epoch: connectionEpoch }
  return [
    {
      ...envelope, type: 'capabilities', event_id: `public-capabilities-${connectionEpoch}`,
      aircraft_firmware: 'unreported', aircraft_model: 'unreported', android_version: '16',
      gimbal_pitch_max_deg: 60, gimbal_pitch_min_deg: -90, horizontal_fov_deg: 82.1,
      measured_hfov_deg: null, media_retrieval: false, native_panorama_modes: [],
      phone_model: 'Example Android phone', photo_capture: false, rc_firmware: 'unreported',
      sdk_version: 'unreported', storage_remaining_bytes: 0,
    },
    {
      ...envelope, type: 'node_status', event_id: `public-node-status-${connectionEpoch}`,
      authority_change_reason: 'not_granted', control_authority: false, phone_battery_percent: 86,
      phone_thermal_state: 'none', video_publish_state: 'stopped', virtual_stick_enabled: false,
      watchdog_state: 'nominal',
    },
    {
      ...envelope, type: 'capture_readiness', event_id: `public-capture-readiness-${connectionEpoch}`,
      room_id: null, capture_id: null, guidance_mode: 'visual_advisory', pose_source: 'unreported',
      pose_ok: false, clearance_ok: false, camera_ok: false, storage_ok: false,
      motion_ok: false, image_quality_ok: false, coverage_missing: [],
      next_heading_deg: null, suggested_delta: null,
    },
  ]
}
