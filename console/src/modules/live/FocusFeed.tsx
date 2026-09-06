import type { DetectionRecord, RequestRecord } from '../../control/state'
import { formatDroneId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import { streamName } from '../../media/playback'
import type { MediaRuntime } from '../../media/runtime'
import type { RelayAircraftState } from '../../relay/contract'
import { membershipTone, type Tone } from '../../shell/derive'
import { formatPercent } from '../../shell/format'
import { deriveCaptureProgress, deriveReadiness, deriveStream } from './derive-live'

export interface FocusFeedProps {
  focused: RelayAircraftState | null
  requests: RequestRecord[]
  detections: DetectionRecord[]
  onAcknowledge: (detectionId: string) => void
  now: number
  media?: MediaRuntime
}

interface Row {
  key: string
  value: string
  tone: Tone
}

/** The focused aircraft at size, its stream label bar, and the nine state rows. */
export function FocusFeed({ focused, requests, detections, onAcknowledge, now, media }: FocusFeedProps) {
  const id = focused ? formatDroneId(focused.drone_id) : 'none'
  return (
    <section data-two="1" aria-label={`Focused aircraft ${id}`}>
      <div className="lv-column">
        {focused ? (
          <Feed drone={focused} now={now} media={media} />
        ) : (
          <div className="lv-feed is-unreported">
            <div className="lv-feed-reticle" aria-hidden="true" />
            <div className="lv-feed-bar">
              <span>none</span>
              <span className="lv-bar-status">
                <span aria-hidden="true" className="lv-dot" />
                unreported
              </span>
              <span>no frame reported</span>
            </div>
            <div className="lv-feed-overlay is-muted">
              No aircraft is focused. Focus a tile on a wall, or select exactly one aircraft.
            </div>
          </div>
        )}
        <p className="lv-stream-note">
          Stream names are derived as <span className="mono">drone{'{id}'}</span>. No adapter-supplied
          media URL is ever rendered.
        </p>
        <h3 className="lv-h3">Detections</h3>
        <p className="lv-det-copy">
          Fresh qualifying sightings promote their source feed. Confirming a sighting records operator
          attention and does not dispatch a flight action.
        </p>
        <DetectionList detections={detections} onAcknowledge={onAcknowledge} />
      </div>
      <div className="lv-column">
        <p className="lv-eyebrow">Focused aircraft</p>
        <p className="lv-id">{id}</p>
        {focused ? (
          <dl className="lv-rows">
            {deriveRows(focused, requests, now).map((row) => (
              <div className="lv-row" key={row.key}>
                <dt>{row.key}</dt>
                <dd className={`tone-${row.tone}`}>{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="lv-none">
            Nothing is focused. Focus follows a single selection; press Focus on a tile to choose
            another aircraft.
          </p>
        )}
      </div>
    </section>
  )
}

function DetectionList({
  detections,
  onAcknowledge,
}: {
  detections: DetectionRecord[]
  onAcknowledge: (detectionId: string) => void
}) {
  if (detections.length === 0) {
    return <p className="lv-det-note" role="status">No fresh detections have arrived.</p>
  }
  return (
    <ul className="lv-detections" aria-label="Detection events">
      {detections.slice(0, 8).map(({ event, acknowledged }) => (
        <li key={event.detection_id} className="lv-detection">
          <p>
            <strong>{event.label}</strong> {Math.round(event.confidence * 100)}% on{' '}
            {formatDroneId(event.drone_id)}
          </p>
          <p className="lv-det-note">
            {event.attention === 'promoted' ? 'Focused for review.' : 'Repeated sighting, logged.'}
          </p>
          <button
            type="button"
            className="lv-detection-ack"
            disabled={acknowledged}
            onClick={() => onAcknowledge(event.detection_id)}
          >
            {acknowledged ? 'Acknowledged' : 'Acknowledge detection'}
          </button>
        </li>
      ))}
    </ul>
  )
}

/**
 * The feed at size. The player mounts only while the relay reports the stream
 * live and playback is configured; every other state is said in words.
 */
function Feed({
  drone,
  now,
  media,
}: {
  drone: RelayAircraftState
  now: number
  media?: MediaRuntime
}) {
  const stream = deriveStream(drone, now)
  const plays = stream.status === 'live' && media !== undefined
  return (
    <div className={`lv-feed is-${stream.status}`}>
      {plays ? (
        <LivePlayer key={drone.drone_id} droneId={drone.drone_id} media={media} />
      ) : (
        <div className="lv-feed-reticle" aria-hidden="true" />
      )}
      <div className="lv-feed-bar">
        <span>{streamName(drone.drone_id)}</span>
        <span className="lv-bar-status">
          <span aria-hidden="true" className={`lv-dot is-${stream.status}`} />
          {stream.status}
        </span>
        <span>{stream.lastFrame}</span>
      </div>
      {stream.degraded && <div className="lv-feed-overlay">{stream.degradedWord}</div>}
      {stream.status === 'live' && media === undefined && (
        <div className="lv-feed-overlay is-muted">
          Playback is not configured on this console. The relay reports the stream live; no media
          bootstrap was provided.
        </div>
      )}
    </div>
  )
}

function deriveRows(drone: RelayAircraftState, requests: RequestRecord[], now: number): Row[] {
  const stream = deriveStream(drone, now)
  const readiness = deriveReadiness(drone)
  const capture = deriveCaptureProgress(requests, drone.drone_id)
  return [
    { key: 'stream status', value: stream.status, tone: stream.tone },
    { key: 'last frame', value: stream.lastFrame, tone: 'muted' },
    { key: 'battery', value: formatPercent(drone.battery), tone: 'ink' },
    { key: 'link', value: formatPercent(drone.link), tone: 'ink' },
    { key: 'position quality', value: formatPercent(drone.pos_quality), tone: 'ink' },
    { key: 'membership', value: drone.membership, tone: membershipTone(drone.membership) },
    { key: 'readiness', value: readiness.text, tone: readiness.tone },
    { key: 'guidance mode', value: 'unreported', tone: 'muted' },
    { key: 'capture progress', value: capture.text, tone: capture.tone },
  ]
}
