import type { DepartureRecord } from '../control/state'
import { capabilityBlockedReason, formatDroneId, isIntentEnabled } from '../control/state'
import type { RelayAircraftState } from '../relay/contract'
import { isReady, membershipTone, metricTone, sortedAircraft } from '../shell/derive'
import { formatPercent, formatTime, humanizeCode } from '../shell/format'
import { MEMBERSHIP_REASON, READINESS } from '../shell/sentences'
import type { ModuleProps } from './types'

/**
 * Registry cards and the departed list, bound to the authoritative state. The
 * context column renders it for every module; Control › Fleet renders it wide.
 */
export function FleetContext({ controller }: ModuleProps) {
  return <FleetRegistry controller={controller} layout="column" />
}

export function FleetRegistry({
  controller,
  layout,
}: {
  controller: ModuleProps['controller']
  layout: 'column' | 'two'
}) {
  const { state, toggleAircraft } = controller
  const fleet = sortedAircraft(state.aircraft)
  const registry = (
    <div>
      {layout === 'two' && (
        <p className="fleet-eyebrow is-first">Registry · roster v{state.rosterVersion}</p>
      )}
      {fleet.length === 0 ? (
        <p className="fleet-empty">
          No aircraft have joined this session. The relay reports an empty roster.
        </p>
      ) : (
        fleet.map((drone) => (
          <FleetCard
            key={drone.drone_id}
            drone={drone}
            selected={state.selection.includes(drone.drone_id)}
            lastInSelection={state.selection.length === 1 && state.selection[0] === drone.drone_id}
            selectionEnabled={isIntentEnabled(state, 'select')}
            selectionDisabledReason={capabilityBlockedReason(state, 'select')}
            onToggle={() => toggleAircraft(drone.drone_id)}
          />
        ))
      )}
    </div>
  )
  const departed = (
    <div>
      <p className={layout === 'two' ? 'fleet-eyebrow is-first' : 'fleet-eyebrow'}>
        Departed this session
      </p>
      {state.departed.length === 0 ? (
        <p className="fleet-none">No aircraft have left.</p>
      ) : (
        state.departed.map((record, index) => (
          <DepartedCard
            key={`${record.drone.drone_id}-${record.t}-${index}`}
            record={record}
            current={state.aircraft[record.drone.drone_id]}
          />
        ))
      )}
      {layout === 'two' && (
        <p className="fleet-footnote">
          Every value here comes from relay state frames. Nothing is estimated between frames.
        </p>
      )}
    </div>
  )
  if (layout === 'two') {
    return (
      <div data-two="1">
        {registry}
        {departed}
      </div>
    )
  }
  return (
    <>
      {registry}
      {departed}
    </>
  )
}

function FleetCard({
  drone,
  selected,
  lastInSelection,
  selectionEnabled,
  selectionDisabledReason,
  onToggle,
}: {
  drone: RelayAircraftState
  selected: boolean
  lastInSelection: boolean
  selectionEnabled: boolean
  selectionDisabledReason: string | null
  onToggle: () => void
}) {
  const id = formatDroneId(drone.drone_id)
  const canSelect = selectionEnabled && isReady(drone)
  const disabled = !canSelect || lastInSelection
  const title = selectionDisabledReason ?? (lastInSelection
    ? 'Intent v1 requires at least one aircraft in a select request.'
    : !canSelect
      ? 'Relay reports this aircraft is not selectable.'
      : undefined)
  return (
    <article className="fleet-card" aria-label={`${id} registry card`}>
      <div className="fleet-card-head">
        <span className="fleet-id">{id}</span>
        <span className={`fleet-membership tone-${membershipTone(drone.membership)}`}>
          {drone.membership}
        </span>
        <span className="fleet-flight">{drone.flight_state ?? 'flight state unreported'}</span>
      </div>
      <div className="fleet-metrics">
        <FleetMetric label="battery" value={drone.battery} />
        <FleetMetric label="link" value={drone.link} />
        <FleetMetric label="position" value={drone.pos_quality} />
      </div>
      <p className="fleet-line">
        <span className={drone.control_authority ? 'tone-ink' : 'tone-danger'}>
          {drone.control_authority ? 'Sweep' : 'RC takeover'}
        </span>
        <span className={drone.rc_safety_operator_present ? undefined : 'tone-danger'}>
          RC safety operator {drone.rc_safety_operator_present ? 'present' : 'absent'}
        </span>
        <span className="mono">epoch {drone.connection_epoch}</span>
        <span className="mono">
          {drone.last_seen_at === null ? 'last seen unreported' : `seen ${formatTime(drone.last_seen_at)}`}
        </span>
      </p>
      {drone.readiness_reasons.length > 0 && (
        <div className="fleet-reasons">
          {drone.readiness_reasons.map((code) => (
            <p key={code}>
              <code>{code}</code> — {READINESS[code] ?? humanizeCode(code)}
            </p>
          ))}
        </div>
      )}
      <button
        type="button"
        className={selected ? 'fleet-select is-selected' : 'fleet-select'}
        aria-pressed={selected}
        aria-label={`${selected ? 'Deselect' : 'Select'} ${id}`}
        disabled={disabled}
        title={title}
        onClick={onToggle}
      >
        {selected ? 'SEL' : canSelect ? 'select' : '—'}
      </button>
    </article>
  )
}

function FleetMetric({ label, value }: { label: string; value: number | null }) {
  const width = value === null ? 0 : Math.round(value * 100)
  return (
    <div>
      <div className="fleet-metric-label">{label}</div>
      <div className={`fleet-metric-value tone-${metricTone(value)}`}>{formatPercent(value)}</div>
      <div className="fleet-metric-track" aria-hidden="true">
        <div className={`fleet-metric-fill tone-${metricTone(value)}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

function DepartedCard({
  record,
  current,
}: {
  record: DepartureRecord
  current: RelayAircraftState | undefined
}) {
  const rejoined = current !== undefined && current.connection_epoch > record.drone.connection_epoch
  return (
    <div className="fleet-departed">
      <p className="fleet-departed-head">
        <span className="fleet-departed-id">{formatDroneId(record.drone.drone_id)}</span>
        <span>epoch {record.drone.connection_epoch}</span>
        <span>{formatTime(record.t)}</span>
      </p>
      <p>
        <code>{record.reasonCode}</code> — {MEMBERSHIP_REASON[record.reasonCode] ?? record.detail}
      </p>
      <p className="fleet-departed-rejoin">
        {rejoined
          ? `Rejoined with connection epoch ${current.connection_epoch}.`
          : 'Has not rejoined.'}
      </p>
    </div>
  )
}
