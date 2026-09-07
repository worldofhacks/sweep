import { membershipWord } from '../../control/observation'
import { deviceNoun, formatDeviceId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import { useCameraChoice } from '../../media/cameras'
import type { MediaRuntime } from '../../media/runtime'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { isReady, membershipTone } from '../../shell/derive'
import { formatPercent } from '../../shell/format'
import { deriveReadiness, deriveStream } from './derive-live'
import { CameraChoice } from './CameraChoice'

export interface MosaicProps {
  devices: RelayAircraftState[]
  now: number
  focusedId: DroneId | null
  selection: DroneId[]
  selectionEnabled?: boolean
  selectionDisabledReason?: string | null
  onFocus: (droneId: DroneId) => void
  onToggleSelection: (droneId: DroneId) => void
  /** Playback runtime; absent means live tiles say playback is not configured. */
  media?: MediaRuntime
}

/**
 * One tile per reported device, keyed by its global ID. A player is torn down
 * with its tile, on inspection/module navigation, or when its stream goes offline.
 */
export function Mosaic({
  devices,
  now,
  focusedId,
  selection,
  selectionEnabled = true,
  selectionDisabledReason = null,
  onFocus,
  onToggleSelection,
  media,
}: MosaicProps) {
  return (
    <section className="lv-wall" aria-label="All devices">
      <p className="lv-note">
        {devices.length} reported {devices.length === 1 ? 'device' : 'devices'}. New devices appear
        automatically; offline feeds keep their place.
      </p>
      <div data-mosaic="1">
        {devices.map((drone) => (
          <Tile
            key={drone.drone_id}
            drone={drone}
            now={now}
            focused={focusedId === drone.drone_id}
            selected={selection.includes(drone.drone_id)}
            lastInSelection={selection.length === 1 && selection[0] === drone.drone_id}
            selectionEnabled={selectionEnabled}
            selectionDisabledReason={selectionDisabledReason}
            onFocus={onFocus}
            onToggleSelection={onToggleSelection}
            media={media}
          />
        ))}
      </div>
    </section>
  )
}

function Tile({
  drone,
  now,
  focused,
  selected,
  lastInSelection,
  selectionEnabled,
  selectionDisabledReason,
  onFocus,
  onToggleSelection,
  media,
}: {
  drone: RelayAircraftState
  now: number
  focused: boolean
  selected: boolean
  lastInSelection: boolean
  selectionEnabled: boolean
  selectionDisabledReason: string | null
  onFocus: (droneId: DroneId) => void
  onToggleSelection: (droneId: DroneId) => void
  media?: MediaRuntime
}) {
  const id = formatDeviceId(drone)
  const noun = deviceNoun(drone.device_class)
  const { cameras, camera, choose } = useCameraChoice(drone)
  const stream = deriveStream(drone, now, camera)
  const readiness = deriveReadiness(drone)
  // Mounted only while the relay says live; unmounting closes the WHEP session.
  const plays = stream.status === 'live' && media !== undefined
  const canSelect = selectionEnabled && isReady(drone)
  const selectLabel = selected ? 'in selection' : canSelect ? 'add to selection' : 'not selectable'
  const selectDisabled = !canSelect || lastInSelection
  const selectTitle = selectionDisabledReason ?? (lastInSelection
    ? `Intent v1 requires at least one ${noun} in a select request.`
    : !canSelect
      ? `Relay reports this ${noun} is not selectable.`
      : undefined)
  return (
    <article className={`lv-tile is-${stream.status}`} aria-label={`${id} camera tile`}>
      <CameraChoice device={drone} cameras={cameras} camera={camera} now={now} onChoose={choose} />
      <div className="lv-visual">
        {plays && camera && <LivePlayer key={`${drone.drone_id}:${drone.connection_epoch}:${camera.camera_id}:${camera.stream}`} device={drone} media={media} camera={drone.cameras === undefined ? undefined : camera} />}
        <div className="lv-bar">
          <span>{id}</span>
          <span className="lv-bar-status">
            <span aria-hidden="true" className={`lv-dot is-${stream.status}`} />
            {stream.status}
          </span>
          <span>{stream.lastFrame}</span>
        </div>
        {stream.degraded && <div className="lv-overlay">{stream.degradedWord}</div>}
        {stream.status === 'live' && media === undefined && (
          <div className="lv-overlay is-muted">Playback is not configured on this console.</div>
        )}
      </div>
      <p className="lv-meta">
        <span className="lv-metric">bat {formatPercent(drone.battery)}</span>
        <span className="lv-metric">link {formatPercent(drone.link)}</span>
        <span className="lv-metric">pos {formatPercent(drone.pos_quality)}</span>
        <span className={`tone-${membershipTone(drone.membership, drone.pos_quality)}`}>{membershipWord(drone)}</span>
        <span className={`tone-${readiness.tone}`}>{readiness.text}</span>
      </p>
      <span className="lv-actions">
        <button
          type="button"
          className="lv-focus"
          aria-label={`Focus ${id}`}
          aria-pressed={focused}
          onClick={() => onFocus(drone.drone_id)}
        >
          Focus
        </button>
        <button
          type="button"
          className={selected ? 'lv-select is-selected' : 'lv-select'}
          aria-label={`${selectLabel} ${id}`}
          aria-pressed={selected}
          disabled={selectDisabled}
          title={selectTitle}
          onClick={() => onToggleSelection(drone.drone_id)}
        >
          {selectLabel}
        </button>
      </span>
    </article>
  )
}
