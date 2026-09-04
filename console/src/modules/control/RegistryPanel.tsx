import { formatDroneId } from '../../control/state'
import type { RelayAircraftState } from '../../relay/contract'
import { humanizeCode } from '../../shell/format'
import { EmptyState, PanelHeading } from '../shared'
import type { ModuleProps } from '../types'

/** The aircraft registry with select toggles and feed focus, moved from the checkpoint dashboard unchanged. */
export function RegistryPanel({
  controller,
  aircraft,
}: {
  controller: ModuleProps['controller']
  aircraft: RelayAircraftState[]
}) {
  const { state, toggleAircraft, selectFeed } = controller
  const selectedFeed =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)
  return (
    <section className="panel registry-panel" aria-labelledby="registry-title">
      <PanelHeading
        eyebrow="Live session"
        title="Aircraft registry"
        meta={`${aircraft.length} known · ${state.selection.length} selected`}
        id="registry-title"
      />
      {aircraft.length === 0 ? (
        <EmptyState
          title="No aircraft state"
          detail="Waiting for an authenticated relay snapshot. No local simulator is running."
        />
      ) : (
        <ul className="aircraft-list">
          {aircraft.map((drone) => {
            const selected = state.selection.includes(drone.drone_id)
            const cannotClearLast = selected && state.selection.length === 1
            const selectable = drone.membership === 'ready' && drone.selectable
            return (
              <li className="aircraft-row" key={drone.drone_id}>
                <button
                  type="button"
                  className={selected ? 'aircraft-selector is-selected' : 'aircraft-selector'}
                  aria-pressed={selected}
                  aria-label={`${formatDroneId(drone.drone_id)} ${humanizeCode(drone.membership)} epoch ${drone.connection_epoch} ${selected ? 'Selected' : 'Select'}`}
                  disabled={!selectable || cannotClearLast}
                  onClick={() => toggleAircraft(drone.drone_id)}
                  title={
                    cannotClearLast
                      ? 'Intent v1 requires at least one aircraft in a select request.'
                      : !selectable
                        ? 'Relay reports this aircraft is not selectable.'
                        : undefined
                  }
                >
                  <span className={`status-dot status-${drone.membership}`} aria-hidden="true" />
                  <span className="aircraft-identity">
                    <strong className="mono">{formatDroneId(drone.drone_id)}</strong>
                    <span>{humanizeCode(drone.membership)}</span>
                  </span>
                  <span className="aircraft-epoch mono">epoch {drone.connection_epoch}</span>
                  <span className="selection-state">{selected ? 'Selected' : 'Select'}</span>
                </button>
                <div className="aircraft-detail">
                  <span className="mono">{drone.adapter_id}</span>
                  <span>{formatReadiness(drone)}</span>
                  <button
                    type="button"
                    className="text-button"
                    aria-label={`View feed ${formatDroneId(drone.drone_id)}`}
                    aria-pressed={selectedFeed?.drone_id === drone.drone_id}
                    onClick={() => selectFeed(drone.drone_id)}
                  >
                    View feed
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function formatReadiness(drone: RelayAircraftState): string {
  if (drone.selectable && drone.readiness_reasons.length === 0) return 'All readiness gates passed'
  if (drone.readiness_reasons.length === 0) return 'Not selectable; relay supplied no readiness reason'
  return drone.readiness_reasons.map(humanizeCode).join(' · ')
}
