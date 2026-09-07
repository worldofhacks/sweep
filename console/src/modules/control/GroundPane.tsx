import { formatDroneId } from '../../control/state'
import { isReady } from '../../shell/derive'
import type { ModuleProps } from '../types'
import { gateControl } from './controls'

const FORWARD_PULSE = { linear_mm_s: 80, angular_mrad_s: 0, duration_ms: 250 }
const TURN_LEFT_PULSE = { linear_mm_s: 0, angular_mrad_s: 350, duration_ms: 250 }
const TURN_RIGHT_PULSE = { linear_mm_s: 0, angular_mrad_s: -350, duration_ms: 250 }

export function GroundPane({ controller }: Pick<ModuleProps, 'controller'>) {
  const { state, issueIntent } = controller
  const selectedId = state.selection.length === 1 ? state.selection[0] : null
  const selected = selectedId === null ? undefined : state.aircraft[selectedId]
  const groundSelected = selected?.node_type === 'ground' ? selected : undefined
  const poseSource = groundSelected?.ground_readiness?.source_id ?? null
  const hasCurrentPose = groundSelected !== undefined && poseSource !== null && Object.values(state.latestObservations).some(
    (observation) => observation.device_id === groundSelected.drone_id &&
      observation.connection_epoch === groundSelected.connection_epoch &&
      observation.source_id === poseSource && observation.payload.kind === 'pose',
  )
  const selectedReason = !groundSelected || !isReady(groundSelected)
    ? 'Select one ready ground node.'
    : !hasCurrentPose
      ? 'The relay has not supplied an accepted ground pose from its declared source.'
      : null
  const pulseReason = selectedReason ?? gateControl(state, 'ground_velocity').reason
  const pulse = (args: typeof FORWARD_PULSE) => {
    if (pulseReason !== null) return
    issueIntent({ name: 'ground_velocity', args })
  }

  return (
    <div className="ct-ground" aria-label="Ground controls">
      <div className="ct-ground-status">
        <p className="ct-eyebrow">Ground readiness</p>
        {groundSelected ? (
          <p>
            {formatDroneId(groundSelected.drone_id)} is {isReady(groundSelected)
              ? 'ready'
              : `not ready (${groundSelected.readiness_reasons.join(', ') || groundSelected.membership})`}; pose source{' '}
            <code>{groundSelected.ground_readiness?.source_id ?? 'unreported'}</code>.
          </p>
        ) : (
          <p>Select one ready ground node. The relay has not selected a usable ground target.</p>
        )}
      </div>

      <section className="ct-ground-section" aria-labelledby="ground-pulse-heading">
        <p id="ground-pulse-heading" className="ct-eyebrow">Pilot pulse</p>
        <div className="ct-static-chips" role="group" aria-label="Ground motion pulses">
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(FORWARD_PULSE)}>
            Forward · 80 mm/s · 250 ms
          </button>
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(TURN_LEFT_PULSE)}>
            Turn left · 350 mrad/s · 250 ms
          </button>
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(TURN_RIGHT_PULSE)}>
            Turn right · 350 mrad/s · 250 ms
          </button>
        </div>
        <p className={`ct-ground-note tone-${pulseReason ? 'warn' : 'muted'}`}>
          {pulseReason ?? 'Each pulse requires confirmation. Keep the local stop within reach.'}
        </p>
      </section>

    </div>
  )
}
