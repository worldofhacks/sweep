import { membershipWord } from '../../control/observation'
import { DeviceTelemetryPanel } from '../devices/DeviceTelemetryPanel'
import type { RequestRecord } from '../../control/state'
import { deviceNoun, formatDeviceId } from '../../control/state'
import { LivePlayer } from '../../media/LivePlayer'
import { useCameraChoice } from '../../media/cameras'
import type { MediaRuntime } from '../../media/runtime'
import type { DeviceCameraState, RelayAircraftState } from '../../relay/contract'
import { membershipTone, type Tone } from '../../shell/derive'
import { formatDeviceLink, formatPercent } from '../../shell/format'
import { deriveCaptureProgress, deriveReadiness, deriveStream } from './derive-live'
import { CameraChoice } from './CameraChoice'
import { useState } from 'react'
import type { LiveDetectionClient } from '../../media/detections'
import { DetectionFeed } from './DetectionFeed'

export interface FocusFeedProps {
  focused: RelayAircraftState | null
  requests: RequestRecord[]
  now: number
  media?: MediaRuntime
  detections?: LiveDetectionClient
  initialDetectionView?: boolean
}

interface Row {
  key: string
  value: string
  tone: Tone
}

/** The focused device at size, its stream label bar, and the nine state rows. */
export function FocusFeed({ focused, requests, now, media, detections, initialDetectionView = false }: FocusFeedProps) {
  const [detectionView, setDetectionView] = useState(initialDetectionView)
  const { cameras, camera, choose } = useCameraChoice(focused)
  const id = focused ? formatDeviceId(focused) : 'none'
  const noun = focused ? deviceNoun(focused.device_class) : 'device'
  return (
    <section data-two="1" aria-label={`Focused ${noun} ${id}`}>
      <div className="lv-column">
        {focused && <CameraChoice device={focused} cameras={cameras} camera={camera} now={now} onChoose={choose} />}
        {detections && <label className="lv-det-copy"><input type="checkbox" checked={detectionView} onChange={(event) => setDetectionView(event.target.checked)} /> Object detection overlay</label>}
        {detectionView && !detections && <p role="status">Object detection service is unavailable on this console.</p>}
        {focused && camera && detectionView && detections && focused.client_observation?.connectionCurrent !== false ? (
          <DetectionFeed key={`${focused.drone_id}:${focused.connection_epoch}:${camera.camera_id}`} client={detections}
            source={{ drone_id: focused.drone_id, connection_epoch: focused.connection_epoch, camera_id: camera.camera_id, stream: camera.stream }} />
        ) : focused ? (
          <Feed drone={focused} now={now} media={media} camera={camera} />
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
        <p className="lv-det-copy">Detection view shows the latest analyzed image with matching boxes and confidence. It updates independently of video playback and never sends a movement command.</p>
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
}: {
  drone: RelayAircraftState
  now: number
  media?: MediaRuntime
  camera: DeviceCameraState | null
}) {
  const stream = deriveStream(drone, now, camera)
  const plays = stream.status === 'live' && media !== undefined
  return (
    <div className={`lv-feed is-${stream.status}`}>
      {plays && camera ? (
        <LivePlayer key={`${drone.drone_id}:${drone.connection_epoch}:${camera.camera_id}:${camera.stream}`} device={drone} media={media} camera={drone.cameras === undefined ? undefined : camera} />
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
