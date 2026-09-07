import { observationCurrent } from '../../control/observation'
import type { DeviceTelemetryValue, RelayAircraftState, RelayCaptureReadinessEvent } from '../../relay/contract'
import type { CaptureReadiness } from '../control/controls'
import { formatAgo } from '../../shell/format'

export const DEVICE_HEALTH_FRESH_MS = 5_000
export type TelemetryRow = { label: string; value: string; tone?: 'ink' | 'warn' | 'danger' | 'muted' }
export const humanLabel = (key: string) => key.replaceAll('_', ' ')
export function displayValue(value: unknown): string {
  if (value === undefined || value === null) return 'unreported'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join(', ') : 'none reported'
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${humanLabel(key)}: ${displayValue(item)}`).join(' · ')
  return String(value)
}
export function reportAge(t: number | null | undefined, now: number): string {
  if (t == null) return 'unreported'
  if (t > now) return 'timestamp ahead of console · freshness unknown'
  return `${formatAgo(now, t)}${now - t > DEVICE_HEALTH_FRESH_MS ? ' · stale, last reported values' : ''}`
}
export function telemetryGroup(device: RelayAircraftState, key: string): Record<string, DeviceTelemetryValue> | null {
  const value = device.node_status?.device_telemetry?.[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}
export function deviceTelemetryRows(device: RelayAircraftState, now: number): TelemetryRow[] {
  now = device.client_observation?.now ?? now
  const node = device.node_status
  const telemetry = device.telemetry
  const camera = device.camera_capabilities
  const stale = !observationCurrent(device) || !node || node.t > now || now - node.t > DEVICE_HEALTH_FRESH_MS || ['disconnected', 'leaving'].includes(device.membership)
  const lidar = telemetryGroup(device, 'lidar')
  const safety = telemetryGroup(device, 'safety')
  const rows: TelemetryRow[] = [
    { label: 'telemetry report', value: reportAge(telemetry?.t ?? device.last_seen_at, now) },
    { label: 'position · m', value: `x ${displayValue(telemetry?.x)} · y ${displayValue(telemetry?.y)} · z ${displayValue(telemetry?.z)}` },
    { label: 'velocity · m/s', value: `x ${displayValue(telemetry?.vx)} · y ${displayValue(telemetry?.vy)} · z ${displayValue(telemetry?.vz)}` },
    { label: 'home · m', value: displayValue(device.home_pose) },
    { label: 'bridge report', value: reportAge(node?.t, now), tone: stale ? 'warn' : 'ink' },
    { label: 'watchdog', value: displayValue(node?.watchdog_state), tone: stale ? 'muted' : node?.watchdog_state === 'nominal' ? 'ink' : 'danger' },
    { label: 'authority reason', value: node?.authority_change_reason ? humanLabel(node.authority_change_reason) : node ? 'none reported' : 'unreported' },
    { label: 'video publisher', value: displayValue(node?.video_publish_state) },
  ]
  if (device.device_class === 'aircraft') rows.push(
    { label: 'virtual stick', value: node ? node.virtual_stick_enabled ? 'enabled (reported)' : 'disabled (reported)' : 'unreported' },
    { label: 'phone battery / thermal', value: node ? `${node.phone_battery_percent}% · ${node.phone_thermal_state}` : 'unreported' },
    { label: 'camera report', value: reportAge(camera?.t, now) },
    { label: 'aircraft / firmware', value: camera ? `${camera.aircraft_model} · ${camera.aircraft_firmware}` : 'unreported' },
    { label: 'RC / phone / SDK', value: camera ? `${camera.rc_firmware} · ${camera.phone_model} · Android ${camera.android_version} · SDK ${camera.sdk_version}` : 'unreported' },
    { label: 'camera / retrieval', value: camera ? `photo ${camera.photo_capture ? 'supported' : 'unsupported'} · retrieval ${camera.media_retrieval ? 'supported' : 'unsupported'} · ${camera.storage_remaining_bytes} bytes remaining` : 'unreported' },
    { label: 'gimbal / field of view', value: camera ? `${camera.gimbal_pitch_min_deg}…${camera.gimbal_pitch_max_deg}° · FOV ${camera.horizontal_fov_deg}° · measured ${displayValue(camera.measured_hfov_deg)}°` : 'unreported' },
    { label: 'capture patterns', value: device.camera_patterns.length ? device.camera_patterns.join(', ') : 'none advertised' },
    { label: 'native panorama modes', value: displayValue(camera?.native_panorama_modes) },
    { label: 'capture guidance', value: device.capture_readiness ? `${device.capture_readiness.guidance_mode} · ${reportAge(device.capture_readiness.t, now)}` : 'unreported' },
  )
  if (device.device_class === 'ground_vehicle' || lidar) rows.push(
    { label: 'obstacle avoidance', value: !safety ? 'unreported · no clearance claim' : stale ? 'last reported safety is stale · clearance unknown' : safety.blocked === true ? `blocked · ${displayValue(safety.reasons)}` : safety.blocked === false ? 'guard reports clear · command admission remains relay-owned' : 'clearance unreported', tone: safety?.blocked === true ? 'danger' : 'warn' },
    { label: 'LiDAR device / health', value: lidar ? `${displayValue(lidar.model)} · present ${displayValue(lidar.present)} · health ${displayValue(lidar.health)} · ${displayValue(lidar.status)}` : 'unreported' },
    { label: 'LiDAR motor', value: lidar ? (lidar.motor_pwm_requested !== undefined ? `requested PWM ${displayValue(lidar.motor_pwm_requested)} · rotation ${displayValue(lidar.motor_running ?? lidar.motor_state ?? lidar.motor_health)}` : displayValue(lidar.motor ?? lidar.motor_running ?? lidar.motor_state ?? lidar.motor_health)) : 'unreported' },
    { label: 'LiDAR scan age', value: typeof lidar?.scan_age_ms === 'number' && node && now >= node.t ? `${Math.round(lidar.scan_age_ms + now - node.t)} ms${stale ? ' · bridge report stale' : ''}` : 'unreported' },
    { label: 'LiDAR coverage', value: lidar ? `${displayValue(lidar.valid_bins)} valid bins · sectors ${displayValue(lidar.sectors)} · nearest ${displayValue(lidar.nearest_m)} m` : 'unreported' },
    { label: 'LiDAR calibration / frame', value: lidar ? `${lidar.calibrated === true ? 'calibrated' : lidar.calibrated === false ? 'uncalibrated' : 'calibration unreported'} · ${displayValue(lidar.frame)}` : 'unreported' },
  )
  return rows
}

/** Only current, selected-device, matching-room advice populates capture gates. */
export function captureGuidance(device: RelayAircraftState | undefined, roomId: string, now: number): CaptureReadiness | null {
  now = device?.client_observation?.now ?? now
  const report: RelayCaptureReadinessEvent | null | undefined = device?.capture_readiness
  if (!device || !observationCurrent(device) || device.device_class !== 'aircraft' || !report || report.connection_epoch !== device.connection_epoch ||
    report.t > now || now - report.t > DEVICE_HEALTH_FRESH_MS ||
    ['leaving', 'disconnected'].includes(device.membership) || (report.room_id !== null && report.room_id !== roomId.trim())) return null
  return { ...report, coverage: Array.from({ length: 8 }, (_, index) => report.coverage_missing.some((angle) => Math.floor(((angle + 22.5) % 360) / 45) === index) ? 'unseen' : 'unreported'),
    suggested_delta: report.suggested_delta ? `${report.suggested_delta.kind} ${report.suggested_delta.degrees}°` : null }
}
