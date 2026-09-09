import { membershipWord } from '../control/observation'
import { DeviceTelemetryPanel } from './devices/DeviceTelemetryPanel'
import type { DepartureRecord } from '../control/state'
import {
  capabilityBlockedReason,
  deviceNoun,
  formatDeviceId,
  isIntentEnabled,
  pluralNoun,
  rosterNoun,
} from '../control/state'
import type { RelayAircraftState, RelaySensorEvent } from '../relay/contract'
import { sensorStatus } from '../sensor/status'
import { useSecondTick } from './live/use-second-tick'
import { useSensorStore } from '../sensor/store'
import { authorityWords, isReady, membershipTone, metricTone, sortedAircraft } from '../shell/derive'
import { formatPercent, formatTime } from '../shell/format'
import { membershipReasonSentence } from '../shell/sentences'
import { motionStateWord } from './control/controls'
import { deviceScan } from './map/derive-map'
import { LidarPolar } from './map/LidarPolar'
import { ReadinessHelp } from './ReadinessHelp'
import { GroundObservation } from './GroundObservation'
import type { ModuleProps } from './types'

/**
 * Registry cards and the departed list, bound to the authoritative state. The
 * context column renders it for every module; Control › Fleet renders it wide.
 */
export function FleetContext({ controller, now }: ModuleProps) {
  return <FleetRegistry controller={controller} layout="column" now={now} />
}

export function FleetRegistry({
  controller,
  layout,
  now,
}: {
  controller: ModuleProps['controller']
  layout: 'column' | 'two'
  now: () => number
}) {
  const { state, toggleAircraft, sensors } = controller
  const snapshot = useSensorStore(sensors)
  const fleet = sortedAircraft(state.aircraft)
  useSecondTick(fleet.length > 0)
  const registry = (
    <div>
      {layout === 'two' && (
        <p className="fleet-eyebrow is-first">Registry · roster v{state.rosterVersion}</p>
      )}
      {fleet.length === 0 ? (
        <p className="fleet-empty">
          No devices have joined this session. The relay reports an empty roster.
        </p>
      ) : (
        fleet.map((drone) => (
          <FleetCard
            key={drone.drone_id}
            drone={drone}
            now={now()}
            selected={state.selection.includes(drone.drone_id)}
            lastInSelection={state.selection.length === 1 && state.selection[0] === drone.drone_id}
            selectionEnabled={isIntentEnabled(state, 'select')}
            selectionDisabledReason={capabilityBlockedReason(state, 'select')}
            scan={deviceScan(drone, snapshot)}
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
        <p className="fleet-none">No {pluralNoun(rosterNoun(fleet))} have left.</p>
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
  now,
  selected,
  lastInSelection,
  selectionEnabled,
  selectionDisabledReason,
  scan,
  onToggle,
}: {
  drone: RelayAircraftState
  now: number
  selected: boolean
  lastInSelection: boolean
  selectionEnabled: boolean
  selectionDisabledReason: string | null
  scan: RelaySensorEvent | null
  onToggle: () => void
}) {
  const id = formatDeviceId(drone)
  const noun = deviceNoun(drone.device_class)
  const words = authorityWords(drone)
  const canSelect = selectionEnabled && isReady(drone)
  const disabled = !canSelect || lastInSelection
  const title = selectionDisabledReason ?? (lastInSelection
    ? `Intent v1 requires at least one ${noun} in a select request.`
    : !canSelect
      ? `Relay reports this ${noun} is not selectable.`
      : undefined)
  return (
    <article className="fleet-card" aria-label={`${id} registry card`}>
      <div className="fleet-card-head">
        <span className="fleet-id">{id}</span>
        <span className={`fleet-membership tone-${membershipTone(drone.membership, drone.pos_quality)}`}>
          {membershipWord(drone)}
        </span>
        <span className="fleet-flight">{motionStateWord(drone)}</span>
      </div>
      <div className="fleet-metrics">
        <FleetMetric label="battery" value={drone.battery} />
        <FleetMetric label="link" value={drone.link} />
        <FleetMetric label="position" value={drone.pos_quality} />
      </div>
      <GroundObservation device={drone} now={now} />
      <p className="fleet-line">
        <span className={drone.control_authority ? 'tone-ink' : 'tone-danger'}>
          {words.authority}
        </span>
        <span className={drone.rc_safety_operator_present ? undefined : 'tone-danger'}>
          {words.operator} {drone.rc_safety_operator_present ? 'present' : 'absent'}
        </span>
        <span className="mono">epoch {drone.connection_epoch}</span>
        <span className="mono">
          {drone.last_seen_at === null ? 'last seen unreported' : `seen ${formatTime(drone.last_seen_at)}`}
        </span>
      </p>
      {drone.device_class === 'ground_vehicle' && <p className={`fleet-line tone-${sensorStatus(drone, now).tone}`}>{sensorStatus(drone, now).text}</p>}
      {drone.device_class === 'ground_vehicle' && drone.adapter_capabilities.includes('lidar') && (
        <LidarPolar device={drone} scan={scan} size={92} now={now} />
      )}
      <ReadinessHelp drone={drone} className="fleet-reasons" />
      {selected && <DeviceTelemetryPanel device={drone} now={now} scan={scan} compact />}
      <button
        type="button"
        className={selected ? 'fleet-select is-selected' : 'fleet-select'}
        aria-pressed={selected}
        aria-label={`${selected ? 'Deselect' : 'Select'} ${id}`}
        disabled={disabled}
        title={title}
        onClick={onToggle}
      >
        {selected ? 'Selected' : canSelect ? 'Select device' : 'Unavailable'}
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
  const noun = deviceNoun(record.drone.device_class)
  return (
    <div className="fleet-departed">
      <p className="fleet-departed-head">
        <span className="fleet-departed-id">{formatDeviceId(record.drone)}</span>
        <span>epoch {record.drone.connection_epoch}</span>
        <span>{formatTime(record.t)}</span>
      </p>
      <p>
        <code>{record.reasonCode}</code> —{' '}
        {membershipReasonSentence(record.reasonCode, noun) ?? record.detail}
      </p>
      <p className="fleet-departed-rejoin">
        {rejoined
          ? `Rejoined with connection epoch ${current.connection_epoch}.`
          : 'Has not rejoined.'}
      </p>
    </div>
  )
}
