import type { NavigationTarget } from '../navigation'
import { isReady } from '../shell/derive'
import { motionObservationCurrent } from './observation'
import { capabilityBlockedReason, formatDeviceId, type ControlState } from './state'

export function navigationTargets(state: ControlState): NavigationTarget[] {
  return state.selection.flatMap((id) => {
    const device = state.aircraft[id]
    return device ? [{ id, deviceClass: device.device_class, epoch: device.connection_epoch }] : []
  })
}

/** Eligibility to request a review; this never authorizes navigation dispatch. */
export function navigationBlockedReason(state: ControlState): string | null {
  if (state.connection.status !== 'connected') return 'Connect to the relay before reviewing a destination.'
  if (state.estop) return 'Clear the active emergency stop before reviewing a destination.'
  const capability = capabilityBlockedReason(state, 'navigate')
  if (capability) return capability
  if (state.selection.length === 0) return 'Select devices to review a destination.'
  for (const id of state.selection) {
    const device = state.aircraft[id]
    if (!device) return `Device ${id} is no longer reported in this session.`
    if (!isReady(device) || !motionObservationCurrent(device) || !device.control_authority) {
      return `${formatDeviceId(device)} needs current readiness, motion telemetry, and control authority.`
    }
    if (device.device_class === 'aircraft' && !['hovering', 'airborne'].includes(device.flight_state ?? '')) {
      return `${formatDeviceId(device)} is not reported airborne and ready for navigation. Takeoff is a separate operation.`
    }
  }
  return null
}
