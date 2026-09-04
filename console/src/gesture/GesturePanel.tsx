import { useEffect, useRef } from 'react'
import './gesture.css'
import { NEVER_GESTURE_EMITTABLE, describeGestureAction, type GesturePair } from './policy'
import { drawLandmarkOverlay } from './overlay'
import {
  useGestureProducer,
  type GestureControlBindings,
  type GestureProducerDependencies,
  type GestureProducerView,
} from './use-gesture-producer'

interface GesturePanelProps {
  control: GestureControlBindings
  roomId: string
  dependencies?: GestureProducerDependencies
}

/**
 * Gesture readout: camera selection, explicit enablement, live video with a
 * landmark overlay, confidence and dwell feedback, the candidate intent
 * preview, the enabled pairs, and the states that emit nothing.
 */
export default function GesturePanel({ control, roomId, dependencies }: GesturePanelProps) {
  const { view, pairs, videoRef, enable, disable, selectDevice, downloadRecording, clearRecording } =
    useGestureProducer({ control, roomId, dependencies })
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    drawLandmarkOverlay(canvasRef.current, videoRef.current, view.frame)
  }, [view.frame, videoRef])

  const pending = control.pendingRequest
  const gesturePreview = pending?.intent.source === 'webcam' ? pending : null
  const candidatePair = 'pair' in view.outcome ? view.outcome.pair : null
  const topScore = view.frame?.hands[0]?.score ?? 0
  const progress = view.outcome.kind === 'candidate' ? view.outcome.progress : view.phase === 'idle' ? 0 : 1
  const duplicateSuppressed =
    view.outcome.kind === 'duplicate_suppressed' || view.phase === 'wait_for_release' || view.phase === 'accepted'

  return (
    <details className="panel gesture-panel">
      <summary className="panel-heading" aria-label="Gesture readout">
        <div>
          <span className="eyebrow">Second input channel</span>
          <h2>Gesture readout</h2>
        </div>
        <span className="panel-meta mono">{humanizeCode(view.status)}</span>
      </summary>

      <div className="gesture-body">
        <div className="gesture-column">
          <div className="gesture-controls">
            <label htmlFor="gesture-camera">Camera</label>
            <select
              id="gesture-camera"
              value={view.selectedDeviceId ?? ''}
              onChange={(event) => void selectDevice(event.target.value || null)}
            >
              <option value="">Browser default</option>
              {view.camera.devices.map((device) => (
                <option key={device.deviceId} value={device.deviceId}>
                  {device.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className={view.enabled ? 'secondary-action' : 'primary-action'}
              aria-pressed={view.enabled}
              onClick={() => (view.enabled ? disable() : void enable())}
            >
              {view.enabled ? 'Disable tracking' : 'Enable tracking'}
            </button>
          </div>

          <div className={`gesture-status is-${view.status}`} role="status">
            <span className="eyebrow">Tracking state</span>
            <strong>{describeStatus(view.status)}</strong>
            {view.statusDetail && <p>{view.statusDetail}</p>}
            {view.status !== 'tracking' && (
              <p className="gesture-safety">
                Emission disabled. The network stop and physical RC remain available.
              </p>
            )}
          </div>

          <div className="gesture-stage">
            <video ref={videoRef} muted playsInline autoPlay aria-label="Webcam preview" />
            <canvas ref={canvasRef} aria-hidden="true" />
            <span className="gesture-stage-label">
              {view.frame ? `${view.frame.hands.length} hand${view.frame.hands.length === 1 ? '' : 's'}` : 'No frames'}
            </span>
          </div>

          <div className="gesture-feedback" aria-label="Gesture feedback">
            <Meter
              label="Confidence"
              value={topScore}
              text={`${Math.round(topScore * 100)}%${candidatePair ? ` / ${Math.round(candidatePair.minScore * 100)}%` : ''}`}
            />
            <Meter
              label="Dwell"
              value={progress}
              text={
                view.outcome.kind === 'candidate'
                  ? `${view.outcome.heldMs} / ${view.outcome.pair.dwellMs} ms`
                  : candidatePair
                    ? `${candidatePair.dwellMs} ms`
                    : '—'
              }
            />
            <div className="gesture-outcome">
              <span className={`status-label status-${view.phase}`}>{humanizeCode(view.phase)}</span>
              {view.frame?.hands[0] && (
                <span className="mono">{view.frame.hands[0].rawCategory ?? 'No gesture'}</span>
              )}
              {duplicateSuppressed && (
                <span className="status-label status-duplicate_suppressed">
                  Duplicate suppressed · release hand to neutral
                </span>
              )}
              {view.notable && view.notable.outcome.kind !== 'duplicate_suppressed' && (
                <span className={`status-label status-${view.notable.outcome.kind}`}>
                  {describeNotable(view.notable.outcome)}
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="gesture-column">
          <div className="gesture-preview" aria-live="polite">
            <span className="eyebrow">Candidate intent preview</span>
            {gesturePreview ? (
              <>
                <strong>
                  {humanizeCode(gesturePreview.intent.name)} ·{' '}
                  <code title={gesturePreview.intent.intent_id}>{shortId(gesturePreview.intent.intent_id)}</code>
                </strong>
                <p>
                  {gesturePreview.plan?.title}. Thumb up confirms and sends through the webcam source;
                  thumb down cancels. Nothing is sent until confirmed.
                </p>
                <pre>{JSON.stringify(gesturePreview.intent, null, 2)}</pre>
              </>
            ) : (
              <>
                <strong>No gesture-drafted preview</strong>
                <p>
                  Hold an open palm to draft capture_room or a closed fist to draft hold. The draft
                  appears in the plan preview above and here; it is never sent without confirmation.
                </p>
              </>
            )}
            {view.lastAction && (
              <p className={view.lastAction.kind === 'blocked' ? 'is-blocked' : undefined}>
                Last gesture action: {humanizeCode(view.lastAction.kind)}. {view.lastAction.detail}
              </p>
            )}
            {view.emissionBlockedReason && view.status === 'tracking' && (
              <p className="is-blocked">Drafting blocked: {view.emissionBlockedReason}</p>
            )}
          </div>

          <div>
            <span className="eyebrow">Enabled gesture-to-intent pairs</span>
            <table className="gesture-pairs">
              <thead>
                <tr>
                  <th>Gesture</th>
                  <th>Action</th>
                  <th>Hold</th>
                  <th>Min score</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map((pair: GesturePair) => (
                  <tr key={pair.gesture}>
                    <td className="mono">{pair.gesture}</td>
                    <td>{describeGestureAction(pair.action)}</td>
                    <td className="mono">{pair.dwellMs} ms</td>
                    <td className="mono">{pair.minScore.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="gesture-never">
              Never gesture-emittable:{' '}
              {NEVER_GESTURE_EMITTABLE.map((name, index) => (
                <span key={name}>
                  {index > 0 ? ', ' : ''}
                  <code>{name}</code>
                </span>
              ))}
              . They stay on the console controls and the physical RC.
            </p>
          </div>

          <div className="gesture-recording">
            <span>
              Session recording: {view.recording.size} entries
              {view.recording.dropped > 0 ? ` (${view.recording.dropped} oldest dropped)` : ''}
            </span>
            <div>
              <button type="button" className="text-button" onClick={downloadRecording}>
                Download session (JSONL)
              </button>
              <button type="button" className="text-button" onClick={clearRecording}>
                Clear recording
              </button>
            </div>
          </div>
        </div>
      </div>
    </details>
  )
}

function Meter({ label, value, text }: { label: string; value: number; text: string }) {
  const percentage = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className="metric-row">
      <span>{label}</span>
      <div className="meter-track" aria-hidden="true">
        <span style={{ width: `${percentage}%` }} />
      </div>
      <strong className="mono">{text}</strong>
    </div>
  )
}

function describeStatus(status: GestureProducerView['status']): string {
  switch (status) {
    case 'disabled':
      return 'Tracking off'
    case 'starting':
      return 'Starting'
    case 'tracking':
      return 'Tracking'
    case 'model_failed_to_load':
      return 'Model failed to load'
    case 'webcam_dropped':
      return 'Webcam dropped'
    case 'permission_denied':
      return 'Camera permission denied'
    case 'camera_unavailable':
      return 'Camera unavailable'
  }
}

function describeNotable(outcome: GestureProducerView['outcome']): string {
  switch (outcome.kind) {
    case 'accepted':
      return `Accepted ${outcome.pair.gesture} after ${outcome.heldMs} ms`
    case 'low_confidence':
      return `Low confidence · ${outcome.pair.gesture} at ${Math.round(outcome.score * 100)}% (needs ${Math.round(outcome.pair.minScore * 100)}%)`
    case 'dwell_timeout':
      return `Dwell timeout · ${outcome.pair.gesture} held ${outcome.heldMs} of ${outcome.pair.dwellMs} ms (${humanizeCode(outcome.reason)})`
    case 'unmapped':
      return `${outcome.category} is not mapped`
    default:
      return humanizeCode(outcome.kind)
  }
}

function humanizeCode(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}
