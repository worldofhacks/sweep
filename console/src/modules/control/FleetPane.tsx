import { membershipWord } from '../../control/observation'
import { sensorStatus } from '../../sensor/status'
import { useSensorStore } from '../../sensor/store'
import { deviceScan } from '../map/derive-map'
import { LidarPolar } from '../map/LidarPolar'
import { useSecondTick } from '../live/use-second-tick'
import type { DepartureRecord } from '../../control/state'
import {
  capabilityBlockedReason,
  deviceNoun,
  formatDeviceId,
  isIntentEnabled,
  pluralNoun,
  rosterNoun,
} from '../../control/state'
import type { RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import { authorityWords, isReady, membershipTone, metricTone, sortedAircraft } from '../../shell/derive'
import { formatAgo, formatPercent, formatTime } from '../../shell/format'
import { membershipReasonSentence } from '../../shell/sentences'
import { ReadinessHelp } from '../ReadinessHelp'
import type { ModuleProps } from '../types'
import { motionStateWord } from './controls'

export interface FleetPaneProps {
  controller: ModuleProps['controller']
  now: () => number
}

/** Control › Fleet: the registry, wide, and the departed list. Every value is from relay state frames. */
export function FleetPane({ controller, now }: FleetPaneProps) {
  const { state, toggleAircraft } = controller
  const snapshot = useSensorStore(controller.sensors)
  const fleet = sortedAircraft(state.aircraft)
  useSecondTick(fleet.some((device) => device.sensor?.last_scan_at != null))
  const at = now()
  return (
    <div data-two="1" className="ct-two">
      <div className="ct-column" role="region" aria-label="Registry">
        <p className="ct-eyebrow ct-fleet-eyebrow">Registry · roster v{state.rosterVersion}</p>
        {fleet.length === 0 ? (
          <p className="ct-registry-empty">
            No devices have joined this session. The relay reports an empty roster.
          </p>
        ) : (
          fleet.map((drone) => (
            <RegistryCard
              key={drone.drone_id}
              drone={drone}
              now={at}
              scan={deviceScan(drone, snapshot)}
              selected={state.selection.includes(drone.drone_id)}
              lastInSelection={state.selection.length === 1 && state.selection[0] === drone.drone_id}
              selectionEnabled={isIntentEnabled(state, 'select')}
              selectionDisabledReason={capabilityBlockedReason(state, 'select')}
              capabilityProfile={state.capabilityProfile}
              onToggle={() => toggleAircraft(drone.drone_id)}
            />
          ))
        )}
      </div>
      <div className="ct-column">
        <p className="ct-eyebrow ct-fleet-eyebrow">Departed this session</p>
        {state.departed.length === 0 ? (
          <p className="ct-departed-none">No {pluralNoun(rosterNoun(fleet))} have left.</p>
        ) : (
          state.departed.map((record, index) => (
            <DepartedCard
              key={`${record.drone.drone_id}-${record.t}-${index}`}
              record={record}
              current={state.aircraft[record.drone.drone_id]}
            />
          ))
        )}
        <p className="ct-fleet-footnote">
          The registry follows the relay's state frame, never telemetry. A departed device returns here with
          a higher connection epoch, and any selection that named it is cleared with the reason stated.
        </p>
      </div>
    </div>
  )
}

function RegistryCard({
  drone,
  now,
  scan,
  selected,
  lastInSelection,
  selectionEnabled,
  selectionDisabledReason,
  capabilityProfile,
  onToggle,
}: {
  drone: RelayAircraftState
  now: number
  scan: RelaySensorEvent | null
  selected: boolean
  lastInSelection: boolean
  selectionEnabled: boolean
  selectionDisabledReason: string | null
  capabilityProfile: string | null
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
    <article className="ct-registry-card" aria-label={`${id} registry card`}>
      <div className="ct-registry-head">
        <span className="ct-registry-id">{id}</span>{' '}
        <span className={`ct-registry-membership tone-${membershipTone(drone.membership, drone.pos_quality)}`}>{membershipWord(drone)}</span>{' '}
        <span className="ct-registry-flight">{motionStateWord(drone)}</span>{' '}
        <span className="ct-registry-stamp">epoch {drone.connection_epoch}</span>{' '}
        <span className="ct-registry-stamp">{formatAgo(now, drone.last_seen_at)}</span>
      </div>
      <div className="ct-registry-metrics">
        <Metric label="battery" value={drone.battery} />
        <Metric label="link" value={drone.link} />
        <Metric label="position" value={drone.pos_quality} />
      </div>
      <p className="ct-registry-authority">
        <span className={drone.control_authority ? 'tone-ink' : 'tone-danger'}>{words.authority}</span>
        <span className={drone.rc_safety_operator_present ? undefined : 'tone-danger'}>
          {words.operator} {drone.rc_safety_operator_present ? 'present' : 'absent'}
        </span>
      </p>
      <p className={`ct-dpad-note tone-${sensorStatus(drone, now).tone}`}>{sensorStatus(drone, now).text}</p>
      {drone.adapter_capabilities.includes('lidar') && <LidarPolar device={drone} scan={scan} now={now} />}
      <div className="ct-registry-patterns" aria-label="Advertised capture patterns">
        {drone.camera_patterns.length === 0 ? (
          <span className="ct-registry-pattern">no capture pattern advertised</span>
        ) : (
          drone.camera_patterns.map((pattern) => (
            <span key={pattern} className="ct-registry-pattern">
              {pattern}
            </span>
          ))
        )}
      </div>
      <ReadinessHelp drone={drone} className="ct-registry-reasons" capabilityProfile={capabilityProfile} />
      <button
        type="button"
        className={selected ? 'ct-registry-select is-selected' : 'ct-registry-select'}
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

function Metric({ label, value }: { label: string; value: number | null }) {
  const tone = metricTone(value)
  return (
    <div>
      <div className="ct-metric-label">{label}</div>
      <div className={`ct-metric-value tone-${tone}`}>{formatPercent(value)}</div>
      <div className="ct-metric-track" aria-hidden="true">
        <div className={`ct-metric-fill tone-${tone}`} style={{ width: `${value === null ? 0 : Math.round(value * 100)}%` }} />
      </div>
    </div>
  )
}

function DepartedCard({ record, current }: { record: DepartureRecord; current: RelayAircraftState | undefined }) {
  const rejoined = current !== undefined && current.connection_epoch > record.drone.connection_epoch
  const id = formatDeviceId(record.drone)
  const noun = deviceNoun(record.drone.device_class)
  return (
    <div className="ct-departed" aria-label={`${id} departed`}>
      <p className="ct-departed-head">
        <span className="ct-departed-id">{id}</span>{' '}
        <span>epoch {record.drone.connection_epoch}</span>{' '}
        <span className="ct-departed-at">{formatTime(record.t)}</span>
      </p>
      <p className="ct-departed-reason">
        <code>{record.reasonCode}</code> —{' '}
        {membershipReasonSentence(record.reasonCode, noun) ?? record.detail}
      </p>
      <p className="ct-departed-rejoin">
        {rejoined
          ? `Rejoined as ${id} with a higher connection epoch (${current.connection_epoch}).`
          : 'Has not rejoined.'}
      </p>
    </div>
  )
}
