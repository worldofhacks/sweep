import { formatDeviceId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import { ambiguousCameraStreams, cameraTarget, cameraTargetKey, type CameraTarget } from '../../media/cameras'
import type { MediaRuntime } from '../../media/runtime'
import type { DeviceCameraState, RelayAircraftState } from '../../relay/contract'
import { EmptyModule } from '../shared'
import { deriveStream } from './derive-live'

export interface CameraMosaicProps {
  devices: RelayAircraftState[]
  now: number
  focused: CameraTarget | null
  onFocus: (target: CameraTarget) => void
  media?: MediaRuntime
}

/** Explicit relay inventory only. No legacy primary-camera or fixed wall-slot guesses. */
export function CameraMosaic({ devices, now, focused, onFocus, media }: CameraMosaicProps) {
  const reported = devices.flatMap((device) => (device.cameras ?? []).map((camera) => ({ device, camera })))
  const unavailable = ambiguousCameraStreams(devices)
  // A duplicate stream cannot truthfully stand in for two independently identified feeds.
  const feeds = reported.filter(({ camera }) => !unavailable.has(camera.stream))
  const ambiguous = reported.length - feeds.length
  const deviceCount = new Set(feeds.map(({ device }) => device.drone_id)).size
  return (
    <section className="lv-wall" aria-label="All cameras">
      <p className="lv-note">
        {feeds.length} configured {feeds.length === 1 ? 'camera' : 'cameras'} across {deviceCount} {deviceCount === 1 ? 'device' : 'devices'}.
        {' '}Source status comes from the relay; playback confirms frames in this browser.
      </p>
      {ambiguous > 0 && <p className="lv-camera-warning" role="status">
        {ambiguous} camera entries share a stream name. They are not played until their mapping is unique.
      </p>}
      {feeds.length === 0 ? <EmptyModule what="configured cameras"
        detail="No unique camera streams are listed by the relay. The device view remains available; no camera slots are invented." /> : (
        <div className="lv-camera-grid">
          {feeds.map(({ device, camera }) => {
            const target = cameraTarget(device, camera)
            const key = cameraTargetKey(target)
            return <CameraTile key={key} device={device} camera={camera} now={now} media={media}
              focused={focused !== null && cameraTargetKey(focused) === key} onFocus={() => onFocus(target)} />
          })}
        </div>
      )}
    </section>
  )
}

function CameraTile({ device, camera, now, focused, onFocus, media }: {
  device: RelayAircraftState
  camera: DeviceCameraState
  now: number
  focused: boolean
  onFocus: () => void
  media?: MediaRuntime
}) {
  const id = formatDeviceId(device)
  const source = deriveStream(device, now, camera)
  const plays = source.status === 'live' && media !== undefined
  return (
    <article className={`lv-tile lv-camera-tile is-${source.status}`} aria-label={`${id} · ${camera.label} (${camera.camera_id}) camera tile`}>
      <header className="lv-camera-heading">
        <h2>{id} · {camera.label}</h2>
        <p>{camera.camera_id} · epoch {device.connection_epoch}</p>
      </header>
      <div className="lv-visual">
        {plays && <LivePlayer device={device} camera={camera} media={media} showPlaybackStatus />}
        <div className="lv-bar">
          <span className="lv-bar-status"><span aria-hidden="true" className={`lv-dot is-${source.status}`} />Source {source.status}</span>
          <span>{source.lastFrame}</span>
        </div>
        {source.degraded && <div className="lv-overlay">{source.degradedWord}</div>}
        {source.status === 'live' && !media && <div className="lv-overlay is-muted">Playback is not configured on this console.</div>}
      </div>
      <p className="lv-camera-stream">Stream <code>{camera.stream}</code></p>
      <button type="button" className="lv-focus" aria-label={`Focus ${id} · ${camera.label} (${camera.camera_id})`}
        aria-pressed={focused} onClick={onFocus}>Inspect camera</button>
    </article>
  )
}
