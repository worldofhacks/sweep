import { useEffect, useRef, useState, type RefObject } from 'react'
import './gesture.css'
import { drawLandmarkOverlay } from '../../gesture/overlay'
import {
  NEVER_GESTURE_EMITTABLE,
  type GestureCategory,
  type GesturePair,
} from '../../gesture/policy'
import {
  useGestureProducer,
  type GestureActionRecord,
  type GestureProducerView,
} from '../../gesture/use-gesture-producer'
import { Pane, type PaneTab } from '../../shell/Pane'
import { formatTime, humanizeCode, shortId } from '../../shell/format'
import type { ModuleProps } from '../types'
import { TargetStrip } from './TargetStrip'

type GesturePane = 'camera' | 'vocab'

const PANES: PaneTab[] = [
  { id: 'camera', label: 'Camera and readout' },
  { id: 'vocab', label: 'Gesture vocabulary' },
]

const READOUT_CAP = 6
/** Consecutive notable outcomes of one kind inside this window collapse into one readout entry. */
const READOUT_COLLAPSE_MS = 1_500

/** Design copy: the five states in which a gesture emits nothing. */
const GESTURE_FAILS: ReadonlyArray<readonly [string, string]> = [
  ['model failed to load', 'The hand model did not load. Gesture emission is disabled.'],
  ['webcam dropped', 'The camera was unplugged or released. Gesture emission is disabled.'],
  ['low confidence', 'Confidence stayed below the threshold. Nothing was emitted.'],
  ['dwell timeout', 'The pose did not hold long enough. Nothing was emitted.'],
  ['duplicate suppressed', 'The same gesture repeated inside the suppression window. Nothing was emitted.'],
]

interface ReadoutEntry {
  id: number
  label: string
  text: string
  at: number
  blocked: boolean
}

interface ReadoutState {
  seq: number
  entries: ReadoutEntry[]
  lastAction: GestureActionRecord | null
  notable: GestureProducerView['notable']
}

/**
 * Gesture recognition: the webcam producer from the gesture branch presented
 * through the design's surface. Tracking is off until the operator enables it;
 * an accepted gesture drafts a preview with source webcam, and thumb gestures
 * confirm or cancel that preview. Every state that emits nothing is shown.
 */
export function GestureModule({ controller, now, roomId, services }: ModuleProps) {
  const [pane, setPane] = useState<GesturePane>('camera')
  const { view, pairs, videoRef, bindVideo, enable, disable, selectDevice, downloadRecording, clearRecording } =
    useGestureProducer({ control: controller, roomId, dependencies: services.gesture })
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const readout = useReadout(view, now)

  useEffect(() => {
    drawLandmarkOverlay(canvasRef.current, videoRef.current, view.frame)
  }, [view.frame, videoRef, pane])

  return (
    <Pane
      title="Gesture recognition"
      note="Live camera in, canonical intents out. Tracking is off until you enable it."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as GesturePane)}
      tabsLabel="Gesture panes"
    >
      <TargetStrip controller={controller} />
      {pane === 'camera' ? (
        <div data-two="1" className="gs-two">
          <div className="gs-column">
            <CameraStage
              view={view}
              bindVideo={bindVideo}
              canvasRef={canvasRef}
            />
            <div className="gs-controls">
              <label>
                Camera
                <select
                  aria-label="Camera"
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
              </label>
              <button
                type="button"
                className="gs-toggle"
                aria-pressed={view.enabled}
                onClick={() => (view.enabled ? disable() : void enable())}
              >
                {view.enabled ? 'Disable tracking' : 'Enable tracking'}
              </button>
              <span className="gs-hint">
                {view.status === 'tracking'
                  ? 'Score must reach 0.80 and the pose must hold for its dwell window.'
                  : 'Off by default. Enabling the camera never bypasses confirmation, the arbiter or the physical RC.'}
              </span>
            </div>
            <TrackingStatus view={view} connection={controller.state.webcamConnection} />
            <Meters view={view} />
            <p className="gs-safety-note">
              estop, arm, takeoff and free-flight motion are never gesture-emittable. The network stop stays
              on its own keyboard connection, and the physical RC pilot stays primary.
            </p>
          </div>

          <div className="gs-column">
            <p className="gs-eyebrow is-first">Gesture-to-intent pairs</p>
            <div className="gs-pairs">
              {pairs.map((pair) => (
                <PairRow key={pair.gesture} pair={pair} view={view} />
              ))}
            </div>
            <NeverEmittable />
            <p className="gs-eyebrow">Candidate intent preview</p>
            <CandidatePreview controller={controller} view={view} />
            <p className="gs-eyebrow">Readout</p>
            <div className="gs-recording">
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
            <div className="gs-log-scroll" data-scroll="1">
            {readout.length === 0 ? (
              <p className="gs-log-empty">
                Nothing recognised yet. Every outcome is recorded here, including the ones that emit nothing.
              </p>
            ) : (
              <ul className="gs-log" aria-label="Gesture readout">
                {readout.map((entry) => (
                  <li key={entry.id}>
                    <p className="gs-log-head">
                      <span className={entry.blocked ? 'gs-log-label is-blocked' : 'gs-log-label'}>{entry.label}</span>
                      <span className="gs-log-time">{formatTime(entry.at)}</span>
                    </p>
                    <p className="gs-log-text">{entry.text}</p>
                  </li>
                ))}
              </ul>
            )}
            </div>
          </div>
        </div>
      ) : (
        <div data-two="1" className="gs-two">
          <div className="gs-column">
            <p className="gs-intro">
              The four MediaPipe poses this console maps. A pose needs a classifier score of 0.80 and its full
              dwell window; confirm and cancel use the shorter 400 ms window so an operator can answer a preview
              quickly.
            </p>
            {pairs.map((pair) => (
              <div key={pair.gesture} className="gs-vocab-row">
                <p className="gs-vocab-head">
                  <span className="gs-vocab-name">{gestureName(pair.gesture)}</span>
                  <span className="gs-vocab-intent">{pair.gesture}</span>
                  <span className="gs-pair-status">{pairStatus(pair)}</span>
                </p>
                <p className="gs-vocab-dwell">{dwellLabel(pair)}</p>
              </div>
            ))}
            <NeverEmittable />
          </div>
          <div className="gs-column">
            <p className="gs-eyebrow is-first">States that emit nothing</p>
            {GESTURE_FAILS.map(([key, value]) => (
              <p key={key} className="gs-fails">
                <span className="is-key">{key}</span>
                <span className="is-value">{value}</span>
              </p>
            ))}
            <p className="gs-note">
              Adversarial target from the PRD: five minutes of fast random hand motion must produce fewer than
              one intent. Dwell, stillness and duplicate suppression are what buy that, not the classifier
              alone.
            </p>
          </div>
        </div>
      )}
    </Pane>
  )
}

function CameraStage({
  view,
  bindVideo,
  canvasRef,
}: {
  view: GestureProducerView
  bindVideo: (element: HTMLVideoElement | null) => void
  canvasRef: RefObject<HTMLCanvasElement | null>
}) {
  const failed = isFailure(view.status)
  const cameraName =
    view.camera.devices.find((device) => device.deviceId === view.camera.deviceId)?.label ?? 'camera 0'
  return (
    <div className="gs-stage">
      <video ref={bindVideo} muted playsInline autoPlay aria-label="Gesture camera preview" />
      <canvas ref={canvasRef} aria-hidden="true" />
      {view.status === 'tracking' && <div aria-hidden="true" className="gs-reticle" />}
      <div className="gs-cam-label">
        <span className="is-camera">{cameraName}</span>
        <span className={view.status === 'tracking' ? 'is-word is-on' : 'is-word'}>{cameraWord(view)}</span>
      </div>
      {failed && view.statusDetail && (
        <p role="alert" className="gs-cam-error">
          {view.statusDetail}
        </p>
      )}
    </div>
  )
}

function TrackingStatus({
  view,
  connection,
}: {
  view: GestureProducerView
  connection: ModuleProps['controller']['state']['webcamConnection']
}) {
  const classes = ['gs-status']
  if (view.status === 'tracking') classes.push('is-tracking')
  if (isFailure(view.status)) classes.push('is-failed')
  return (
    <div className={classes.join(' ')} role="status" aria-label="Tracking state">
      <strong>{describeStatus(view.status)}</strong>
      {view.statusDetail && <p>{view.statusDetail}</p>}
      {view.status !== 'tracking' && (
        <p className="gs-safety">Emission disabled. The network stop and physical RC remain available.</p>
      )}
      <p className="gs-source">
        webcam source {connection.status}
        {connection.status !== 'connected' && connection.reason ? ` — ${connection.reason}` : ''}
      </p>
    </div>
  )
}

function Meters({ view }: { view: GestureProducerView }) {
  const candidatePair = 'pair' in view.outcome ? view.outcome.pair : null
  const topScore = view.frame?.hands[0]?.score ?? 0
  const progress =
    view.outcome.kind === 'candidate' ? view.outcome.progress : view.phase === 'idle' ? 0 : 1
  const duplicateSuppressed =
    view.outcome.kind === 'duplicate_suppressed' ||
    view.phase === 'wait_for_release' ||
    view.phase === 'accepted'
  return (
    <div className="gs-meters" aria-label="Gesture feedback">
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
      <div className="gs-outcome">
        <span>{humanizeCode(view.phase)}</span>
        {view.frame?.hands[0] && <span className="mono">{view.frame.hands[0].rawCategory ?? 'No gesture'}</span>}
        {duplicateSuppressed && <span className="tone-warn">Duplicate suppressed · release hand to neutral</span>}
        {view.notable && view.notable.outcome.kind !== 'duplicate_suppressed' && (
          <span className={view.notable.outcome.kind === 'accepted' ? 'tone-ok' : 'tone-warn'}>
            {describeNotable(view.notable.outcome)}
          </span>
        )}
      </div>
    </div>
  )
}

function Meter({ label, value, text }: { label: string; value: number; text: string }) {
  const percentage = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <div className="gs-meter">
      <span className="gs-meter-label">{label}</span>
      <div className="gs-meter-track" aria-hidden="true">
        <span className="gs-meter-fill" style={{ width: `${percentage}%` }} />
      </div>
      <strong className="gs-meter-value">{text}</strong>
    </div>
  )
}

function PairRow({ pair, view }: { pair: GesturePair; view: GestureProducerView }) {
  const active = 'pair' in view.outcome && view.outcome.pair.gesture === pair.gesture
  const pct =
    active && view.outcome.kind === 'candidate'
      ? Math.round(view.outcome.progress * 100)
      : active && view.outcome.kind === 'accepted'
        ? 100
        : 0
  const dwelling = active && view.outcome.kind === 'candidate'
  return (
    <div className={dwelling ? 'gs-pair is-dwelling' : 'gs-pair'} aria-label={`${gestureName(pair.gesture)} pair`}>
      <span aria-hidden="true" className="gs-pair-fill" style={{ width: `${pct}%` }} />
      <span className="gs-pair-name">{gestureName(pair.gesture)}</span>
      <span className="gs-pair-status">{pairStatus(pair)}</span>
      <span className="gs-pair-dwell">{dwellLabel(pair)}</span>
    </div>
  )
}

function NeverEmittable() {
  return (
    <p className="gs-never">
      Never gesture-emittable:{' '}
      {NEVER_GESTURE_EMITTABLE.map((name, index) => (
        <span key={name}>
          {index > 0 ? ', ' : ''}
          <code>{name}</code>
        </span>
      ))}
      . They stay on the console controls and the physical RC.
    </p>
  )
}

function CandidatePreview({
  controller,
  view,
}: {
  controller: ModuleProps['controller']
  view: GestureProducerView
}) {
  const pending = controller.pendingRequest
  const gesturePreview = pending?.intent.source === 'webcam' ? pending : null
  return (
    <div className="gs-preview" aria-live="polite">
      {gesturePreview ? (
        <>
          <strong>
            {humanizeCode(gesturePreview.intent.name)} ·{' '}
            <code title={gesturePreview.intent.intent_id}>{shortId(gesturePreview.intent.intent_id)}</code>
          </strong>
          <p>
            {gesturePreview.plan?.title}. Thumb up confirms and sends through the webcam source; thumb down
            cancels. Nothing is sent until confirmed.
          </p>
        </>
      ) : (
        <>
          <strong>No gesture-drafted preview</strong>
          <p>
            Hold an open palm to draft capture_room or a closed fist to draft hold. The draft appears in the
            dock and here; it is never sent without confirmation.
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
  )
}

/** Accumulates the readout log from the producer's latest action and notable outcome. */
function useReadout(view: GestureProducerView, now: () => number): ReadoutEntry[] {
  const [readout, setReadout] = useState<ReadoutState>({
    seq: 0,
    entries: [],
    lastAction: null,
    notable: null,
  })
  if (readout.lastAction !== view.lastAction || readout.notable !== view.notable) {
    let seq = readout.seq
    let entries = readout.entries
    const at = now()
    if (view.notable !== readout.notable && view.notable && view.notable.outcome.kind !== 'accepted') {
      const collapse =
        readout.notable !== null &&
        readout.notable.outcome.kind === view.notable.outcome.kind &&
        view.notable.t - readout.notable.t < READOUT_COLLAPSE_MS
      if (!collapse) {
        seq += 1
        entries = [{ id: seq, at, ...notableEntry(view.notable.outcome) }, ...entries]
      }
    }
    if (view.lastAction !== readout.lastAction && view.lastAction) {
      seq += 1
      entries = [{ id: seq, at, ...actionEntry(view.lastAction) }, ...entries]
    }
    setReadout({
      seq,
      entries: entries.slice(0, READOUT_CAP),
      lastAction: view.lastAction,
      notable: view.notable,
    })
  }
  return readout.entries
}

function actionEntry(action: GestureActionRecord): Omit<ReadoutEntry, 'id' | 'at'> {
  const label =
    action.kind === 'draft'
      ? 'drafted'
      : action.kind === 'confirm'
        ? 'confirmed'
        : action.kind === 'cancel'
          ? 'cancelled'
          : 'blocked'
  return { label, text: action.detail, blocked: action.kind === 'blocked' }
}

function notableEntry(outcome: GestureProducerView['outcome']): Omit<ReadoutEntry, 'id' | 'at'> {
  switch (outcome.kind) {
    case 'low_confidence':
      return {
        label: 'low confidence',
        text: `${gestureName(outcome.pair.gesture)} scored ${Math.round(outcome.score * 100)}%; the threshold is ${Math.round(outcome.pair.minScore * 100)}%. Nothing was emitted.`,
        blocked: true,
      }
    case 'dwell_timeout':
      return {
        label: 'dwell timeout',
        text: `${gestureName(outcome.pair.gesture)} held ${outcome.heldMs} of ${outcome.pair.dwellMs} ms (${humanizeCode(outcome.reason).toLowerCase()}). Nothing was emitted.`,
        blocked: true,
      }
    case 'duplicate_suppressed':
      return {
        label: 'duplicate suppressed',
        text: `${gestureName(outcome.pair.gesture)} repeated before the hand returned to neutral. Nothing was emitted.`,
        blocked: true,
      }
    case 'unmapped':
      return {
        label: 'unmapped',
        text: `${gestureName(outcome.category)} is not mapped to an intent. Nothing was emitted.`,
        blocked: true,
      }
    default:
      return { label: humanizeCode(outcome.kind).toLowerCase(), text: 'Nothing was emitted.', blocked: true }
  }
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

function cameraWord(view: GestureProducerView): string {
  if (view.status === 'tracking') return 'tracking enabled'
  if (view.status === 'disabled') return 'tracking disabled (default)'
  if (view.status === 'starting') return 'starting'
  return 'tracking failed'
}

function isFailure(status: GestureProducerView['status']): boolean {
  return (
    status === 'model_failed_to_load' ||
    status === 'webcam_dropped' ||
    status === 'permission_denied' ||
    status === 'camera_unavailable'
  )
}

function describeNotable(outcome: GestureProducerView['outcome']): string {
  switch (outcome.kind) {
    case 'accepted':
      return `Accepted ${gestureName(outcome.pair.gesture)} after ${outcome.heldMs} ms`
    case 'low_confidence':
      return `Low confidence · ${gestureName(outcome.pair.gesture)} at ${Math.round(outcome.score * 100)}% (needs ${Math.round(outcome.pair.minScore * 100)}%)`
    case 'dwell_timeout':
      return `Dwell timeout · ${gestureName(outcome.pair.gesture)} held ${outcome.heldMs} of ${outcome.pair.dwellMs} ms (${humanizeCode(outcome.reason).toLowerCase()})`
    case 'unmapped':
      return `${gestureName(outcome.category)} is not mapped`
    default:
      return humanizeCode(outcome.kind)
  }
}

function pairStatus(pair: GesturePair): string {
  if (pair.action.kind === 'draft') return `emits ${pair.action.name} as a preview`
  return pair.action.kind === 'confirm' ? 'confirms the pending preview' : 'cancels the pending preview'
}

function dwellLabel(pair: GesturePair): string {
  return `${pair.dwellMs} ms dwell · score ${pair.minScore.toFixed(2)}`
}

/** "Open_Palm" reads as "Open palm", the way the design names poses. */
function gestureName(category: GestureCategory): string {
  const words = category.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase()
}
