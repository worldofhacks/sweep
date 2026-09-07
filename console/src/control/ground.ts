import type { ConsoleIntentName, DroneId, GroundVelocityArgs } from '../relay/contract'
import { isReady } from '../shell/derive'
import { capabilityBlockedReason, type ControlState } from './state'

export type GroundDirection = 'forward' | 'left' | 'right'
/** Requested pulse parameters, not measured speed or distance guarantees. */
export function groundPulseArgs(direction: GroundDirection): GroundVelocityArgs {
  return { linear_mm_s: direction === 'forward' ? 80 : 0,
    angular_mrad_s: direction === 'left' ? 350 : direction === 'right' ? -350 : 0, duration_ms: 250 }
}

export function hasGroundTarget(state: ControlState, ids: readonly DroneId[]): boolean {
  return ids.some((id) => state.aircraft[id]?.node_type === 'ground')
}

/** A local pose remains local. Only the explicit ground dispatcher may move it. */
export function groundControlBlockedReason(state: ControlState, name: ConsoleIntentName, ids = state.selection): string | null {
  if (name !== 'ground_velocity' && !hasGroundTarget(state, ids)) return null
  if (['hold', 'estop', 'select'].includes(name)) return null
  if (name !== 'ground_velocity' && name !== 'come_home') return 'This ground runtime supports bounded pulses and its configured return only. Use Ground controls.'
  if (ids.length !== 1 || state.aircraft[ids[0]]?.node_type !== 'ground') return 'Select exactly one ground robot.'
  if (!['connected', 'degraded'].includes(state.connection.status)) return 'The console connection is unavailable.'
  const capability = capabilityBlockedReason(state, name)
  if (capability) return capability
  if (state.estop) return 'The network stop is active.'
  const device = state.aircraft[ids[0]]
  if (!isReady(device) || !device.control_authority || !device.adapter_capabilities.includes('ground_drive')) {
    return 'Wait for an accepted current ground pose and a subsequent ready snapshot with drive authority.'
  }
  return null
}
