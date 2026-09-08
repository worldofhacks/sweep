import { membershipWord } from '../../control/observation'
import { DeviceTelemetryPanel } from '../devices/DeviceTelemetryPanel'
import type { RequestRecord } from '../../control/state'
import { deviceNoun, formatDeviceId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import { cameraTarget, useCameraChoice, useCameraInspection, type CameraTarget } from '../../media/cameras'
import type { MediaRuntime } from '../../media/runtime'
import type { DeviceCameraState, RelayAircraftState } from '../../relay/contract'
import { membershipTone, type Tone } from '../../shell/derive'
import { formatDeviceLink, formatPercent } from '../../shell/format'
import { deriveCaptureProgress, deriveReadiness, deriveStream } from './derive-live'
import { CameraChoice } from './CameraChoice'

export interface FocusFeedProps {
  focused: RelayAircraftState | null
  requests: RequestRecord[]
  now: number
  media?: MediaRuntime
  /** Camera-wall inspection keeps its exact connection and stream identity. */
  target?: CameraTarget | null
  onTargetChange?: (target: CameraTarget) => void
  unavailableStreams?: ReadonlySet<string>
}

interface Row {
  key: string
  value: string
  tone: Tone
}

/** The focused device at size, its stream label bar, and the nine state rows. */
export function FocusFeed({ focused, requests, now, media, target, onTargetChange, unavailableStreams }: FocusFeedProps) {
  const { cameras, camera: deviceCamera, choose } = useCameraChoice(focused, unavailableStreams)
  const inspectedCamera = useCameraInspection(focused, target, unavailableStreams)
  const camera = target ? inspectedCamera : deviceCamera
  const chooseCamera = (cameraId: string) => {
    if (!target) { choose(cameraId); return }
    const selected = focused?.cameras?.find((entry) => entry.camera_id === cameraId)
    if (focused && selected) onTargetChange?.(cameraTarget(focused, selected))
  }
  const id = focused ? formatDeviceId(focused) : 'none'
  const noun = focused ? deviceNoun(focused.device_class) : 'device'
  return (
    <section data-two="1" aria-label={`Focused ${noun} ${id}`}>
      <div className="lv-column">
        {focused && !camera && cameras.length > 0 && <p className="lv-camera-warning" role="status">
          The inspected camera connection is no longer available. Choose a current camera or return to the wall.
        </p>}
        {focused && <CameraChoice device={focused} cameras={cameras} camera={camera} now={now} onChoose={chooseCamera} unavailableStreams={unavailableStreams} />}
        {focused ? (
          <Feed drone={focused} now={now} media={media} camera={camera} showPlaybackStatus={Boolean(target)} />
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
              No device is focused. Return to All devices and focus a tile, or select exactly one device.
            </div>
          </div>
        )}
        <p className="lv-stream-note">
          Each camera uses its configured stream and reports its own freshness.
          Switching cameras changes this view only; it does not select or command another device.
        </p>
        <h3 className="lv-h3">Detections</h3>
        <p className="lv-det-copy">
          Shown at 0.6 and above. At 0.8 and above the device's feed is promoted to focus within one
          second. A detection never emits a command — the operator decides.
        </p>
        <p className="lv-det-note" role="status">
          The relay does not report detections on this console yet. Nothing is shown rather than a
          fixture.
        </p>
      </div>
      <div className="lv-column">
        <p className="lv-eyebrow">Focused {noun}</p>
        <p className="lv-id">{id}</p>
        {focused ? (
          <dl className="lv-rows">
            {deriveRows(focused, requests, now, camera).map((row) => (
              <div className="lv-row" key={row.key}>
                <dt>{row.key}</dt>
                <dd className={`tone-${row.tone}`}>{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="lv-none">
            Nothing is focused. Focus follows a single selection; press Focus on a tile to choose
            another device.
          </p>
        )}
        {focused && <DeviceTelemetryPanel device={focused} now={now} />}
      </div>
    </section>
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
  camera,
  showPlaybackStatus,
}: {
  drone: RelayAircraftState
  now: number
  media?: MediaRuntime
  camera: DeviceCameraState | null
  showPlaybackStatus?: boolean
}) {
  const stream = deriveStream(drone, now, camera)
  const plays = stream.status === 'live' && media !== undefined
  return (
    <div className={`lv-feed is-${stream.status}`}>
      {plays && camera ? (
        <LivePlayer key={`${drone.drone_id}:${drone.connection_epoch}:${camera.camera_id}:${camera.stream}`} device={drone} media={media} camera={drone.cameras === undefined ? undefined : camera} showPlaybackStatus={showPlaybackStatus} />
      ) : (
        <div className="lv-feed-reticle" aria-hidden="true" />
      )}
      <div className="lv-feed-bar">
        <span>{camera?.label ?? 'No camera configured'}</span>
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

function deriveRows(drone: RelayAircraftState, requests: RequestRecord[], now: number, camera: DeviceCameraState | null): Row[] {
  const stream = deriveStream(drone, now, camera)
  const readiness = deriveReadiness(drone)
  const capture = deriveCaptureProgress(requests, drone.drone_id)
  return [
    { key: 'stream status', value: stream.status, tone: stream.tone },
    { key: 'last frame', value: stream.lastFrame, tone: 'muted' },
    { key: 'battery', value: formatPercent(drone.battery), tone: 'ink' },
    { key: 'link', value: formatDeviceLink(drone), tone: 'ink' },
    { key: 'position quality', value: formatPercent(drone.pos_quality), tone: 'ink' },
    { key: 'membership', value: membershipWord(drone), tone: membershipTone(drone.membership) },
    { key: 'readiness', value: readiness.text, tone: readiness.tone },
    { key: 'guidance mode', value: drone.capture_readiness?.guidance_mode ?? 'unreported', tone: 'muted' },
    { key: 'capture progress', value: capture.text, tone: capture.tone },
  ]
}
