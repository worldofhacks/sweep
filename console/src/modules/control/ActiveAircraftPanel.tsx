import { formatDroneId } from '../../control/state'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { humanizeCode } from '../../shell/format'
import { PanelHeading } from '../shared'

/** The first two aircraft's authoritative state, moved from the checkpoint dashboard unchanged. */
export function ActiveAircraftPanel({
  activeAircraft,
  selection,
}: {
  activeAircraft: RelayAircraftState[]
  selection: DroneId[]
}) {
  return (
    <section className="panel active-panel" aria-labelledby="active-title">
      <PanelHeading eyebrow="Authoritative state" title="Active aircraft" meta="First two" id="active-title" />
      <div className="active-state-grid">
        {[0, 1].map((slot) => {
          const drone = activeAircraft[slot]
          return drone ? (
            <AircraftStateCard
              drone={drone}
              selected={selection.includes(drone.drone_id)}
              key={drone.drone_id}
            />
          ) : (
            <div className="drone-state empty-slot" key={`empty-${slot}`}>
              <span className="mono">SLOT {slot + 1}</span>
              <strong>Awaiting aircraft</strong>
              <p>No authoritative state available.</p>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function AircraftStateCard({ drone, selected }: { drone: RelayAircraftState; selected: boolean }) {
  return (
    <article className="drone-state">
      <header>
        <div>
          <strong className="mono">{formatDroneId(drone.drone_id)}</strong>
          {selected && <span className="selected-tag">Selected</span>}
        </div>
        <span className={`status-label status-${drone.membership}`}>{humanizeCode(drone.membership)}</span>
      </header>
      <p className="flight-state">{drone.flight_state ?? 'Awaiting telemetry'}</p>
      <Metric label="Battery" value={drone.battery} />
      <Metric label="Link" value={drone.link} />
      <Metric label="Position" value={drone.pos_quality} />
      <dl className="state-facts">
        <div><dt>Epoch</dt><dd className="mono">{drone.connection_epoch}</dd></div>
        <div><dt>Control</dt><dd>{drone.control_authority ? 'Granted' : 'Missing'}</dd></div>
        <div><dt>RC safety</dt><dd>{drone.rc_safety_operator_present ? 'Present' : 'Missing'}</dd></div>
      </dl>
    </article>
  )
}

function Metric({ label, value }: { label: string; value: number | null }) {
  const percentage = value === null ? 0 : Math.round(value * 100)
  return (
    <div className="metric-row">
      <span>{label}</span>
      <div className="meter-track" aria-hidden="true"><span style={{ width: `${percentage}%` }} /></div>
      <strong className="mono">{value === null ? '—' : `${percentage}%`}</strong>
    </div>
  )
}
