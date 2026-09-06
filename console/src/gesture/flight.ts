import { capabilityBlockedReason, formatDroneId, type ControlState, type RequestRecord } from '../control/state'
import type { IntentRequest } from '../control/use-control-console'
import type { FlightDraftAction } from './policy'

export const BODY_PULSE_SPEED_MM_S = 250
export const BODY_PULSE_DURATION_MS = 500
export const BODY_PULSE_CAPABILITY = 'body_pulse_v1'

export function flightActionLabel(action: FlightDraftAction): string {
  if (action.name === 'arm') return 'Arm session'
  if (action.name === 'takeoff') return 'Takeoff selected'
  if (action.name === 'land') return 'Land selected'
  return action.direction === 'forward' ? 'Forward 0.5 seconds' : 'Backward 0.5 seconds'
}

export function flightIntentRequest(action: FlightDraftAction, selection: number[]): IntentRequest {
  return {
    name: action.name,
    args: action.name === 'body_pulse'
      ? { forward_mm_s: action.direction === 'forward' ? BODY_PULSE_SPEED_MM_S : -BODY_PULSE_SPEED_MM_S, duration_ms: BODY_PULSE_DURATION_MS }
      : {},
    targets: [...selection],
  }
}

/** Shared button/gesture readiness; the relay independently checks the command. */
export function flightActionBlockedReason(
  state: ControlState,
  pending: RequestRecord | null,
  action: FlightDraftAction,
): string | null {
  if (state.connection.status !== 'connected') return `The console connection is ${state.connection.status}.`
  if (pending) return 'Confirm or cancel the pending preview first.'
  const capability = capabilityBlockedReason(state, action.name)
  if (capability) return capability
  if (state.selection.length === 0) return 'Select at least one ready aircraft.'
  const notReady = state.selection.find((id) => state.aircraft[id]?.membership !== 'ready' || !state.aircraft[id]?.selectable)
  if (notReady !== undefined) return `${formatDroneId(notReady)} is not ready or selectable.`
  if (action.name === 'arm') return state.armed ? 'The session is already enabled.' : null
  if (state.estop) return 'The emergency stop is active.'
  if (!state.armed) return 'Arm session first; this enables commands without starting motors.'
  if (action.name === 'body_pulse') {
    const unsupported = state.selection.find((id) => !state.aircraft[id].adapter_capabilities.includes(BODY_PULSE_CAPABILITY))
    if (unsupported !== undefined) return `${formatDroneId(unsupported)} does not advertise ${BODY_PULSE_CAPABILITY}.`
    const grounded = state.selection.find((id) => !['airborne', 'hovering'].includes(state.aircraft[id].flight_state ?? ''))
    if (grounded !== undefined) return `${formatDroneId(grounded)} must report airborne or hovering before a pulse.`
  }
  return null
}
