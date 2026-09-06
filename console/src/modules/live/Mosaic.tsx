import { formatDroneId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import type { MediaRuntime } from '../../media/runtime'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { isReady, membershipTone } from '../../shell/derive'
import { formatPercent } from '../../shell/format'
import { deriveReadiness, deriveStream, mosaicNote, mosaicSlots, type WallSize } from './derive-live'

export interface MosaicProps {
  aircraft: RelayAircraftState[]
  count: WallSize
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
 * The wall of four or six: one tile per reported aircraft, empty slots stay
 * empty. Every tile whose stream the relay reports live hosts its own player,
 * so the Wall of 4 holds four concurrent WHEP sessions; a player is torn down
 * with its tile, on pane change, and the moment the relay stops saying live.
 */
export function Mosaic({
  aircraft,
  count,
  now,
  focusedId,
  selection,
  selectionEnabled = true,
  selectionDisabledReason = null,
  onFocus,
  onToggleSelection,
  media,
}: MosaicProps) {
  const slots = mosaicSlots(aircraft, count)
  return (
    <section className="lv-wall" aria-label={`Wall of ${count}`}>
      <p className="lv-note">{mosaicNote(count, aircraft.length)}</p>
      <div data-mosaic="1">
        {slots.map((drone, index) =>
          drone ? (
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
          ) : (
            <EmptySlot key={`slot-${index + 1}`} slot={index + 1} />
          ),
        )}
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
  const id = formatDroneId(drone.drone_id)
  const stream = deriveStream(drone, now)
  const readiness = deriveReadiness(drone)
  // Mounted only while the relay says live; unmounting closes the WHEP session.
  const plays = stream.status === 'live' && media !== undefined
  const canSelect = selectionEnabled && isReady(drone)
  const selectLabel = selected ? 'in selection' : canSelect ? 'add to selection' : 'not selectable'
  const selectDisabled = !canSelect || lastInSelection
  const selectTitle = selectionDisabledReason ?? (lastInSelection
    ? 'Intent v1 requires at least one aircraft in a select request.'
    : !canSelect
      ? 'Relay reports this aircraft is not selectable.'
      : undefined)
  return (
    <article className={`lv-tile is-${stream.status}`} aria-label={`${id} camera tile`}>
      <div className="lv-visual">
        {plays && <LivePlayer key={drone.drone_id} droneId={drone.drone_id} media={media} />}
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
        <span className={`tone-${membershipTone(drone.membership)}`}>{drone.membership}</span>
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

function EmptySlot({ slot }: { slot: number }) {
  return (
    <article className="lv-tile is-empty" aria-label={`Slot ${slot} empty`}>
      <div className="lv-visual">
        <div className="lv-bar">
          <span>slot {slot}</span>
          <span className="lv-bar-status">
            <span aria-hidden="true" className="lv-dot" />
            unreported
          </span>
          <span>no aircraft</span>
        </div>
      </div>
      <p className="lv-meta">
        <span className="tone-muted">No aircraft reported for this slot.</span>
      </p>
    </article>
  )
}
