import { observationCurrent } from './observation'
import { isCameraControlArgs, type CameraControlArgs, type DeviceTelemetryValue, type RelayAircraftState } from '../relay/contract'
import { capabilityBlockedReason, type ControlState } from './state'

export function cameraTelemetryGroup(device: RelayAircraftState, key: string): Record<string, DeviceTelemetryValue> | null {
  const value = device.node_status?.device_telemetry?.[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

/** Camera controls target one connected aircraft without changing fleet motion selection. */
export function cameraControlBlockedReason(state: ControlState, device: RelayAircraftState | undefined, args: CameraControlArgs, now: number): string | null {
  if (state.connection.status !== 'connected') return 'The console connection is unavailable.'
  const capability = capabilityBlockedReason(state, 'camera_control')
  if (capability) return capability
  if (device && !observationCurrent(device)) return 'Current device state is unavailable; wait for a fresh connection report.'
  if (!device || device.device_class !== 'aircraft' || !['registered', 'ready', 'degraded'].includes(device.membership)) return 'Requires a connected aircraft.'
  if (!device.adapter_capabilities.includes('camera_control_v1')) return 'This adapter does not support camera controls.'
  if (!isCameraControlArgs(args)) return 'Enter a valid camera setting within the displayed limits.'
  now = device.client_observation?.now ?? now
  const node = device.node_status
  if (!node || now < node.t || now - node.t > 5_000) return 'Waiting for fresh aircraft camera telemetry.'
  if (state.estop || !device.control_authority || !node.control_authority || !device.rc_safety_operator_present || node.watchdog_state !== 'nominal') return 'Requires current aircraft control authority, a safety operator and a nominal watchdog.'
  const controls = cameraTelemetryGroup(device, 'controls')
  const operations = controls?.supported_operations
  const operation = { ready: 'camera_ready', photo: 'capture_photo', gimbal: 'set_gimbal_pitch' }[args.kind]
  if (!Array.isArray(operations) || !operations.includes(operation)) return 'The connected SDK has not reported support for this camera action.'
  if (args.kind === 'gimbal') {
    const gimbal = cameraTelemetryGroup(device, 'gimbal')
    const min = gimbal?.pitch_min_deg
    const max = gimbal?.pitch_max_deg
    if (typeof min !== 'number' || typeof max !== 'number' || !Number.isFinite(min) || !Number.isFinite(max) || min >= max) return 'Waiting for the SDK to report the gimbal pitch range.'
    if (args.pitch_mdeg < min * 1000 || args.pitch_mdeg > max * 1000) return `Pitch must be within the reported ${min}° to ${max}° range.`
  }
  return null
}
