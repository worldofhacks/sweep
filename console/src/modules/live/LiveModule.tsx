import { useMemo } from 'react'
import { formatDroneId } from '../../control/state'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { Pane } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { formatPercent, formatTime, humanizeCode } from '../../shell/format'
import { PanelHeading } from '../shared'
import type { ModuleProps } from '../types'

/** Camera mosaic and focus pane, moved from the checkpoint dashboard unchanged. */
export function LiveModule({ controller }: ModuleProps) {
  const { state, selectFeed } = controller
  const aircraft = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  const selectedFeed =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)
  return (
    <Pane
      title="Live view"
      note="Every reported camera source with its focus pane. Detections are not reported yet."
    >
      <div data-two="1">
        <section className="panel mosaic-panel" aria-labelledby="mosaic-title">
          <PanelHeading
            eyebrow="Authoritative media state"
            title="Camera mosaic"
            meta={`${aircraft.length} sources · fixture first`}
            id="mosaic-title"
          />
          <div className="camera-mosaic" aria-label="Camera sources">
            {aircraft.map((drone) => (
              <CameraTile
                drone={drone}
                focused={selectedFeed?.drone_id === drone.drone_id}
                key={drone.drone_id}
                onFocus={selectFeed}
              />
            ))}
          </div>
        </section>

        <section className="panel focus-panel" aria-labelledby="focus-title">
          <PanelHeading
            eyebrow="Operator focus"
            title="Focus pane"
            meta={selectedFeed ? formatDroneId(selectedFeed.drone_id) : 'No source selected'}
            id="focus-title"
          />
          <FocusPane drone={selectedFeed} />
        </section>
      </div>
    </Pane>
  )
}

function CameraTile({
  drone,
  focused,
  onFocus,
}: {
  drone: RelayAircraftState
  focused: boolean
  onFocus: (droneId: DroneId) => void
}) {
  const source = mediaSource(drone)
  const classes = ['camera-tile', `is-${source.status}`]
  if (focused) classes.push('is-focused')
  return (
    <article className={classes.join(' ')}>
      <div className="camera-tile-visual" aria-hidden="true">
        <span className="cam-label">
          <span className={`camera-signal is-${source.status}`} />
          <span>{source.label}</span>
        </span>
      </div>
      <div className="camera-tile-header">
        <div>
          <strong className="mono">{formatDroneId(drone.drone_id)}</strong>
          <span>{source.label}</span>
        </div>
        <button
          type="button"
          className="text-button"
          aria-pressed={focused}
          onClick={() => onFocus(drone.drone_id)}
        >
          Focus {formatDroneId(drone.drone_id)}
        </button>
      </div>
      <dl className="camera-tile-metrics">
        <div><dt>Battery</dt><dd>{formatPercent(drone.battery)}</dd></div>
        <div><dt>Link</dt><dd>{formatPercent(drone.link)}</dd></div>
        <div><dt>Position</dt><dd>{formatPercent(drone.pos_quality)}</dd></div>
      </dl>
      <p className="camera-tile-detail">
        {humanizeCode(drone.membership)}
        {drone.readiness_reasons.length > 0 ? ` · ${drone.readiness_reasons.map(humanizeCode).join(', ')}` : ''}
        {' · '}
        {formatLastFrame(source.lastFrameAt)}
      </p>
    </article>
  )
}

function FocusPane({ drone }: { drone: RelayAircraftState | null }) {
  if (!drone) {
    return (
      <div className="focus-empty">
        <span className="reticle" aria-hidden="true" />
        <strong>Select one aircraft or focus a tile</strong>
        <p>No media source is focused. Flight controls are unaffected.</p>
      </div>
    )
  }
  const source = mediaSource(drone)
  return (
    <section className={`focus-source is-${source.status}`} aria-label={`Focused camera ${formatDroneId(drone.drone_id)}`}>
      <div className="focus-source-visual" aria-hidden="true">
        <span className="reticle" />
        <span className="cam-label">
          <span className={`camera-signal is-${source.status}`} />
          <span>{streamName(drone.drone_id)}</span>
        </span>
      </div>
      <div className="focus-source-copy">
        <span className="eyebrow">{source.label}</span>
        <strong>{formatDroneId(drone.drone_id)} · stream {streamName(drone.drone_id)}</strong>
        <p>{formatLastFrame(source.lastFrameAt)}. Browser playback stays held for M3.1 media integration.</p>
        <dl>
          <div><dt>Flight</dt><dd>{drone.flight_state ?? 'Awaiting telemetry'}</dd></div>
          <div><dt>Health</dt><dd>{humanizeCode(drone.membership)}</dd></div>
          <div><dt>Epoch</dt><dd className="mono">{drone.connection_epoch}</dd></div>
        </dl>
      </div>
    </section>
  )
}

function mediaSource(drone: RelayAircraftState) {
  const video = drone.video
  if (!video || video.status === 'unreported') {
    return {
      status: 'unreported' as const,
      label: 'Stream unreported',
      lastFrameAt: video?.last_frame_at ?? null,
    }
  }
  if (video.status === 'offline') {
    return { status: 'offline' as const, label: 'Video offline', lastFrameAt: video.last_frame_at }
  }
  return { status: 'live' as const, label: 'Live fixture source', lastFrameAt: video.last_frame_at }
}

function streamName(droneId: DroneId): string {
  return `drone${droneId}`
}

function formatLastFrame(value: number | null): string {
  return value === null ? 'No frame timestamp' : `Last frame ${formatTime(value)}`
}
