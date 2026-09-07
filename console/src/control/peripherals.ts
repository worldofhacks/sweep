import { observationCurrent } from './observation'
import type { RelayAircraftState, RobotPeripheralArgs } from '../relay/contract'
import { telemetryGroup } from '../modules/devices/telemetry'
import { capabilityBlockedReason, type ControlState } from './state'

/** This target path never changes the fleet's motion selection or grants wheel authority. */
export function peripheralBlockedReason(state: ControlState, device: RelayAircraftState | undefined, kind: RobotPeripheralArgs['kind'], now = Date.now()): string | null {
  if (state.connection.status !== 'connected') return 'The console connection is unavailable.'
  const capability = capabilityBlockedReason(state, 'robot_peripheral')
  if (capability) return capability
  if (device && !observationCurrent(device)) return 'Current device state is unavailable; wait for a fresh connection report.'
  if (!device || device.device_class !== 'ground_vehicle' || !['registered', 'ready', 'degraded'].includes(device.membership)) return 'Requires a connected robot.'
  if (!device.adapter_capabilities.includes('robot_peripheral_v1') || !device.adapter_capabilities.includes(kind)) return `${kind} is not supported by this connected adapter.`
  now = device.client_observation?.now ?? now
  if (kind === 'neck') {
    const status = device.node_status
    if (state.estop || !status || now < status.t || now - status.t > 5000 || status.watchdog_state !== 'nominal' || telemetryGroup(device, 'safety')?.motion_enabled !== true) return 'Neck movement requires fresh local enable status, a nominal watchdog and the network stop clear.'
  }
  return null
}
