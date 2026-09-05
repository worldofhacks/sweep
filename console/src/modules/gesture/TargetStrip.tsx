import { formatDroneId } from '../../control/state'
import { isReady, sortedAircraft } from '../../shell/derive'
import { formatPercent, humanizeCode } from '../../shell/format'
import type { ModuleProps } from '../types'

/**
 * The target strip the design shows above the Gesture and Speech panes: who is
 * selected, chips that toggle selection through the relay, and the aircraft
 * that cannot be commanded. "All ready" drafts a select preview for the dock.
 */
export function TargetStrip({ controller }: { controller: ModuleProps['controller'] }) {
  const { state, pendingRequest, toggleAircraft, prepareSelect } = controller
  const fleet = sortedAircraft(state.aircraft)
  const ready = fleet.filter(isReady).map((drone) => drone.drone_id)
  const blockers = fleet.filter((drone) => !isReady(drone))
  const allReadySelected = ready.length > 0 && ready.every((id) => state.selection.includes(id))
  const allReadyDisabled = ready.length === 0 || allReadySelected || pendingRequest !== null
  return (
    <div className="tg-strip" role="group" aria-label="Target">
      <span className="tg-strip-count">
        <span className="tg-eyebrow">Target</span>
        <span className="tg-strip-selected">
          {state.selection.length} of {fleet.length} selected
        </span>
      </span>
      <span className="tg-strip-chips">
        {fleet.map((drone) => {
          const can = isReady(drone)
          const on = state.selection.includes(drone.drone_id)
          const lastSelected = on && state.selection.length === 1
          const reason = can
            ? lastSelected
              ? 'Intent v1 requires at least one aircraft in a select request.'
              : undefined
            : humanizeCode(drone.readiness_reasons[0] ?? drone.membership)
          const classes = ['tg-chip']
          if (on) classes.push('is-selected')
          if (!can) classes.push('is-blocked')
          return (
            <button
              key={drone.drone_id}
              type="button"
              className={classes.join(' ')}
              aria-pressed={on}
              aria-label={`${on ? 'Deselect' : 'Select'} ${formatDroneId(drone.drone_id)}`}
              disabled={!can || lastSelected}
              title={reason}
              onClick={() => toggleAircraft(drone.drone_id)}
            >
              <span className="tg-chip-id">{formatDroneId(drone.drone_id)}</span>
              <span className="tg-chip-sub">
                {drone.flight_state ?? 'unreported'} · {formatPercent(drone.battery)}
              </span>
            </button>
          )
        })}
        <button
          type="button"
          className="tg-strip-all"
          disabled={allReadyDisabled}
          title={
            pendingRequest
              ? 'Confirm or cancel the pending preview first.'
              : allReadySelected
                ? 'Every ready aircraft is already selected.'
                : ready.length === 0
                  ? 'No aircraft is ready.'
                  : 'Drafts a select preview for every ready aircraft.'
          }
          onClick={() => prepareSelect(ready, 'console')}
        >
          All ready
        </button>
      </span>
      {blockers.length > 0 && (
        <span className="tg-strip-blockers">
          <span aria-hidden="true" className="tone-danger">
            ▲{' '}
          </span>
          {blockers
            .map(
              (drone) =>
                `${formatDroneId(drone.drone_id)} ${humanizeCode(drone.readiness_reasons[0] ?? 'not selectable').toLowerCase()}`,
            )
            .join(' · ')}{' '}
          — these cannot be selected or commanded.
        </span>
      )}
    </div>
  )
}
