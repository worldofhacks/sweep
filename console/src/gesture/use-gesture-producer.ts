/**
 * Binds the webcam camera, the MediaPipe recognizer, and the pure policy to the
 * existing control flow. An accepted draft gesture becomes a previewed Intent v1
 * draft with source `webcam` through the same factory and reducer as the
 * buttons: capture_room and hold through their prepare functions, takeoff, the
 * forward translate step, and land through issueIntent, which parks every
 * webcam request in the dock. An accepted confirm or cancel gesture acts on
 * that pending preview. Nothing is sent without a preview, the intent_id
 * assigned at draft time is kept through confirmation, and the confirmed
 * intent leaves through the webcam-bound relay client. Tracking is off until
 * the operator enables it.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { formatDroneId, type ControlState, type RequestRecord } from '../control/state'
import type { IntentRequest } from '../control/use-control-console'
import type { ConsoleIntentName, IntentV1 } from '../relay/contract'
import { createCameraController, type CameraController, type CameraState } from './camera'
import {
  DEFAULT_GESTURE_POLICY_CONFIG,
  GESTURE_TRANSLATE_STEP,
  createGesturePolicyState,
  describeGestureDraft,
  stepGesturePolicy,
  type GestureEmittableName,
  type GesturePair,
  type GesturePhase,
  type GesturePolicyConfig,
  type GesturePolicyOutcome,
  type GesturePolicyState,
} from './policy'
import {
  ModelLoadError,
  createMediaPipeGestureSource,
  type GestureFrame,
  type GestureSource,
  type RecognizerStatus,
} from './recognizer'
import { createSessionRecorder, type SessionRecorder } from './recorder'

/** The slice of useControlConsole the producer drives. */
export interface GestureControlBindings {
  state: ControlState
  pendingRequest: RequestRecord | null
  prepareCapture(roomId: string, source: 'webcam'): IntentV1 | null
  prepareHold(source: 'webcam'): IntentV1 | null
  /** The buttons' path; with source webcam the request is always parked in the dock. */
  issueIntent<N extends ConsoleIntentName>(request: IntentRequest<N>): IntentV1 | null
  confirmRequest(intentId: string): IntentV1 | null
  cancelRequest(intentId: string): void
}

/**
 * Drafts one gesture-emittable name through the control flow with source
 * webcam. Every branch parks the draft in the dock; none sends.
 */
export function draftGestureIntent(
  bindings: GestureControlBindings,
  name: GestureEmittableName,
  roomId: string,
): IntentV1 | null {
  switch (name) {
    case 'capture_room':
      return bindings.prepareCapture(roomId, 'webcam')
    case 'hold':
      return bindings.prepareHold('webcam')
    case 'takeoff':
      return bindings.issueIntent({ name: 'takeoff', args: {}, source: 'webcam' })
    case 'translate':
      return bindings.issueIntent({ name: 'translate', args: { ...GESTURE_TRANSLATE_STEP }, source: 'webcam' })
    case 'land':
      return bindings.issueIntent({ name: 'land', args: {}, source: 'webcam' })
  }
}

export interface GestureClock {
  /** Monotonic milliseconds for dwell and frame gaps. */
  monotonic(): number
  /** Epoch milliseconds for the session recording. */
  wall(): number
}

export interface GestureProducerDependencies {
  camera: CameraController
  createSource(): GestureSource
  clock: GestureClock
  /** Schedules one frame callback; returns a cancel. Browser: requestVideoFrameCallback or rAF. */
  scheduleFrame(video: HTMLVideoElement, callback: () => void): () => void
  attachStream(video: HTMLVideoElement, stream: MediaStream | null): Promise<void>
  downloadFile(name: string, contents: string): void
  policy: GesturePolicyConfig
  recorderCapacity?: number
}

export function createBrowserGestureDependencies(): GestureProducerDependencies {
  return {
    camera: createCameraController(),
    createSource: () => createMediaPipeGestureSource(),
    clock: { monotonic: () => performance.now(), wall: () => Date.now() },
    scheduleFrame: (video, callback) => {
      const withVideoFrames = video as HTMLVideoElement & {
        requestVideoFrameCallback?: (callback: () => void) => number
        cancelVideoFrameCallback?: (handle: number) => void
      }
      if (withVideoFrames.requestVideoFrameCallback && withVideoFrames.cancelVideoFrameCallback) {
        const handle = withVideoFrames.requestVideoFrameCallback(callback)
        return () => withVideoFrames.cancelVideoFrameCallback?.(handle)
      }
      const handle = requestAnimationFrame(callback)
      return () => cancelAnimationFrame(handle)
    },
    attachStream: async (video, stream) => {
      video.srcObject = stream
      if (!stream) return
      try {
        await video.play()
      } catch {
        // Autoplay can be refused before the element is visible; the frame loop retries on readiness.
      }
    },
    downloadFile: (name, contents) => {
      const blob = new Blob([contents], { type: 'application/x-ndjson' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    },
    policy: DEFAULT_GESTURE_POLICY_CONFIG,
  }
}

export type GestureProducerStatus =
  | 'disabled'
  | 'starting'
  | 'tracking'
  | 'model_failed_to_load'
  | 'webcam_dropped'
  | 'permission_denied'
  | 'camera_unavailable'

export interface GestureActionRecord {
  t: number
  kind: 'draft' | 'confirm' | 'cancel' | 'blocked'
  detail: string
  intentId: string | null
}

export interface GestureProducerView {
  enabled: boolean
  status: GestureProducerStatus
  statusDetail: string | null
  camera: CameraState
  recognizer: RecognizerStatus
  recognizerDetail: string | null
  selectedDeviceId: string | null
  /** Latest recognizer frame, for the landmark overlay. */
  frame: GestureFrame | null
  /** Latest per-frame policy outcome, for the confidence and dwell meters. */
  outcome: GesturePolicyOutcome
  phase: GesturePhase
  /** The last outcome worth calling out: accepted, low confidence, dwell timeout, duplicate. */
  notable: { outcome: GesturePolicyOutcome; t: number } | null
  lastAction: GestureActionRecord | null
  /** Why an accepted gesture would emit nothing right now, or null when it could act. */
  emissionBlockedReason: string | null
  recording: { size: number; dropped: number }
}

export interface UseGestureProducerOptions {
  control: GestureControlBindings
  roomId: string
  dependencies?: GestureProducerDependencies
}

const NOTABLE_KINDS = new Set<GesturePolicyOutcome['kind']>([
  'accepted',
  'low_confidence',
  'dwell_timeout',
  'duplicate_suppressed',
  'unmapped',
])

export function useGestureProducer({ control, roomId, dependencies }: UseGestureProducerOptions) {
  const [deps] = useState(() => dependencies ?? createBrowserGestureDependencies())
  const [recorder] = useState<SessionRecorder>(() =>
    createSessionRecorder({
      sessionId: control.state.sessionId,
      pairs: deps.policy.pairs,
      wallClock: deps.clock.wall,
      capacity: deps.recorderCapacity,
    }),
  )
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const controlRef = useRef(control)
  const roomIdRef = useRef(roomId)
  const enabledRef = useRef(false)
  const sourceRef = useRef<GestureSource | null>(null)
  const policyRef = useRef<GesturePolicyState>(createGesturePolicyState())
  const cancelFrameRef = useRef<(() => void) | null>(null)
  const lastRecordedOutcomeRef = useRef<GesturePolicyOutcome['kind'] | null>(null)
  const recognizerRef = useRef<RecognizerStatus>('unloaded')

  const [enabled, setEnabled] = useState(false)
  const [camera, setCamera] = useState<CameraState>(deps.camera.state)
  const [recognizer, setRecognizerState] = useState<{ status: RecognizerStatus; detail: string | null }>(
    { status: 'unloaded', detail: null },
  )
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  const [frameView, setFrameView] = useState<{
    frame: GestureFrame | null
    outcome: GesturePolicyOutcome
    phase: GesturePhase
    notable: GestureProducerView['notable']
  }>({ frame: null, outcome: { kind: 'idle' }, phase: 'idle', notable: null })
  const [lastAction, setLastAction] = useState<GestureActionRecord | null>(null)
  const [recording, setRecording] = useState({ size: 0, dropped: 0 })

  useLayoutEffect(() => {
    controlRef.current = control
    roomIdRef.current = roomId
  })

  const setRecognizer = useCallback((status: RecognizerStatus, detail: string | null = null) => {
    recognizerRef.current = status
    setRecognizerState({ status, detail })
  }, [])

  const syncRecording = useCallback(() => {
    setRecording({ size: recorder.size, dropped: recorder.dropped })
  }, [recorder])

  const recordStatus = useCallback(
    (detail: string | null) => {
      recorder.record({
        kind: 'status',
        t: deps.clock.monotonic(),
        enabled: enabledRef.current,
        camera: deps.camera.state.status,
        recognizer: recognizerRef.current,
        detail,
      })
      syncRecording()
    },
    [deps, recorder, syncRecording],
  )

  const stopLoop = useCallback(() => {
    cancelFrameRef.current?.()
    cancelFrameRef.current = null
    policyRef.current = createGesturePolicyState()
    lastRecordedOutcomeRef.current = null
    setFrameView((previous) =>
      previous.phase === 'idle' && previous.outcome.kind === 'idle'
        ? previous
        : { ...previous, outcome: { kind: 'idle' }, phase: 'idle' },
    )
  }, [])

  const act = useCallback(
    (pair: GesturePair, t: number) => {
      const bindings = controlRef.current
      const blocked = emissionBlockedReason(bindings, pair, roomIdRef.current)
      const describe = (kind: GestureActionRecord['kind'], detail: string, intentId: string | null) => {
        const record: GestureActionRecord = { t, kind, detail, intentId }
        setLastAction(record)
        return record
      }
      if (blocked) {
        recorder.record({
          kind: 'intent',
          t,
          event: 'blocked',
          intent_id: bindings.pendingRequest?.intent.intent_id ?? null,
          name: pair.action.kind === 'draft' ? pair.action.name : null,
          detail: blocked,
        })
        describe('blocked', blocked, null)
        return
      }
      if (pair.action.kind === 'draft') {
        const intent = draftGestureIntent(bindings, pair.action.name, roomIdRef.current)
        const drafted = describeGestureDraft(pair.action.name)
        const detail = intent
          ? `${pair.gesture} drafted ${drafted} for preview; nothing sent.`
          : `${pair.gesture} could not draft ${drafted}; the control flow refused it.`
        recorder.record({
          kind: 'intent',
          t,
          event: intent ? 'draft' : 'blocked',
          intent_id: intent?.intent_id ?? null,
          name: pair.action.name,
          detail,
          ...(intent ? { intent } : {}),
        })
        describe(intent ? 'draft' : 'blocked', detail, intent?.intent_id ?? null)
        return
      }
      const pending = bindings.pendingRequest as RequestRecord
      if (pair.action.kind === 'confirm') {
        const confirmed = bindings.confirmRequest(pending.intent.intent_id)
        const detail = confirmed
          ? `${pair.gesture} confirmed ${confirmed.name}; sent through the webcam source.`
          : `${pair.gesture} confirmation was refused; the preview was invalidated.`
        recorder.record({
          kind: 'intent',
          t,
          event: confirmed ? 'confirm' : 'blocked',
          intent_id: pending.intent.intent_id,
          name: pending.intent.name,
          detail,
          ...(confirmed ? { intent: confirmed } : {}),
        })
        describe(confirmed ? 'confirm' : 'blocked', detail, pending.intent.intent_id)
        return
      }
      bindings.cancelRequest(pending.intent.intent_id)
      const detail = `${pair.gesture} cancelled the ${pending.intent.name} preview; nothing sent.`
      recorder.record({
        kind: 'intent',
        t,
        event: 'cancel',
        intent_id: pending.intent.intent_id,
        name: pending.intent.name,
        detail,
      })
      describe('cancel', detail, pending.intent.intent_id)
    },
    [recorder],
  )

  const processFrame = useCallback(() => {
    const video = videoRef.current
    const source = sourceRef.current
    if (!video || !source || !enabledRef.current) return
    if (deps.camera.state.status !== 'streaming' || recognizerRef.current !== 'ready') return
    const t = deps.clock.monotonic()
    let frame: GestureFrame | null
    try {
      frame = source.recognize(video, t)
    } catch (error) {
      const detail = error instanceof ModelLoadError ? error.message : 'The gesture recognizer failed.'
      setRecognizer('model_failed_to_load', detail)
      stopLoop()
      recordStatus(detail)
      return
    }
    if (!frame) return

    recorder.record({ kind: 'recognizer', t, hands: frame.hands })
    const hand = frame.hands[0]
    const step = stepGesturePolicy(
      policyRef.current,
      { t, category: hand?.category ?? null, score: hand?.score ?? 0 },
      deps.policy,
    )
    const phaseChanged = step.state.phase !== policyRef.current.phase
    policyRef.current = step.state
    if (
      phaseChanged ||
      step.outcome.kind !== lastRecordedOutcomeRef.current ||
      step.outcome.kind === 'accepted'
    ) {
      recorder.record({ kind: 'policy', t, phase: step.state.phase, outcome: step.outcome })
      lastRecordedOutcomeRef.current = step.outcome.kind
    }
    if (step.outcome.kind === 'accepted') act(step.outcome.pair, t)
    syncRecording()
    setFrameView((previous) => ({
      frame,
      outcome: step.outcome,
      phase: step.state.phase,
      notable: NOTABLE_KINDS.has(step.outcome.kind) ? { outcome: step.outcome, t } : previous.notable,
    }))
  }, [act, deps, recordStatus, recorder, setRecognizer, stopLoop, syncRecording])

  const startLoop = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    cancelFrameRef.current?.()
    const loop = () => {
      processFrame()
      if (!enabledRef.current || cancelFrameRef.current === null) return
      cancelFrameRef.current = deps.scheduleFrame(video, loop)
    }
    cancelFrameRef.current = deps.scheduleFrame(video, loop)
  }, [deps, processFrame])

  useEffect(() => {
    const unsubscribe = deps.camera.subscribe((state) => {
      setCamera(state)
      if (state.status === 'webcam_dropped' || state.status === 'permission_denied' || state.status === 'unavailable') {
        stopLoop()
        recordStatus(state.detail)
      }
    })
    void deps.camera.refreshDevices()
    return unsubscribe
  }, [deps, recordStatus, stopLoop])

  const disable = useCallback(() => {
    if (!enabledRef.current) return
    enabledRef.current = false
    setEnabled(false)
    stopLoop()
    deps.camera.stop()
    streamRef.current = null
    sourceRef.current?.close()
    sourceRef.current = null
    setRecognizer('unloaded')
    const video = videoRef.current
    if (video) void deps.attachStream(video, null)
    setFrameView({ frame: null, outcome: { kind: 'idle' }, phase: 'idle', notable: null })
    recordStatus('Tracking disabled by the operator.')
  }, [deps, recordStatus, setRecognizer, stopLoop])

  const enable = useCallback(async () => {
    if (enabledRef.current) return
    enabledRef.current = true
    setEnabled(true)
    setLastAction(null)
    recordStatus('Tracking enabled by the operator.')

    const stream = await deps.camera.start(selectedDeviceId)
    if (!enabledRef.current) return
    if (!stream) {
      recordStatus(deps.camera.state.detail)
      return
    }
    streamRef.current = stream
    const video = videoRef.current
    if (video) await deps.attachStream(video, stream)
    if (!enabledRef.current) return

    setRecognizer('loading')
    const source = deps.createSource()
    sourceRef.current = source
    try {
      await source.load()
    } catch (error) {
      if (!enabledRef.current) return
      const detail = error instanceof Error ? error.message : 'The gesture model failed to load.'
      sourceRef.current = null
      source.close()
      deps.camera.stop()
      setRecognizer('model_failed_to_load', detail)
      recordStatus(detail)
      return
    }
    if (!enabledRef.current || sourceRef.current !== source) {
      source.close()
      return
    }
    setRecognizer('ready')
    recordStatus('Recognizer ready.')
    startLoop()
  }, [deps, recordStatus, selectedDeviceId, setRecognizer, startLoop])

  const selectDevice = useCallback(
    async (deviceId: string | null) => {
      setSelectedDeviceId(deviceId)
      if (!enabledRef.current) return
      stopLoop()
      const stream = await deps.camera.start(deviceId)
      if (!enabledRef.current) return
      streamRef.current = stream
      const video = videoRef.current
      if (video) await deps.attachStream(video, stream)
      if (stream && recognizerRef.current === 'ready') startLoop()
    },
    [deps, startLoop, stopLoop],
  )

  /**
   * Callback ref for the preview element. The working pane remounts its
   * children on every sub-tab switch, so a returning video element gets the
   * live stream re-attached and the frame loop restarted without touching
   * the camera or the recognizer.
   */
  const bindVideo = useCallback(
    (element: HTMLVideoElement | null) => {
      const previous = videoRef.current
      videoRef.current = element
      if (!element || element === previous || !enabledRef.current) return
      const stream = streamRef.current
      if (stream && element.srcObject !== stream) void deps.attachStream(element, stream)
      if (stream && recognizerRef.current === 'ready' && deps.camera.state.status === 'streaming') {
        startLoop()
      }
    },
    [deps, startLoop],
  )

  useEffect(() => {
    const unmount = () => disable()
    return unmount
  }, [disable])

  const downloadRecording = useCallback(() => {
    deps.downloadFile(`gesture-session-${control.state.sessionId}.jsonl`, recorder.toJsonl())
  }, [control.state.sessionId, deps, recorder])

  const clearRecording = useCallback(() => {
    recorder.clear()
    syncRecording()
  }, [recorder, syncRecording])

  const status = deriveStatus(enabled, camera, recognizer.status, recognizer.detail)
  const view: GestureProducerView = {
    enabled,
    status: status.status,
    statusDetail: status.detail ?? recognizer.detail ?? camera.detail,
    camera,
    recognizer: recognizer.status,
    recognizerDetail: recognizer.detail,
    selectedDeviceId,
    frame: frameView.frame,
    outcome: frameView.outcome,
    phase: frameView.phase,
    notable: frameView.notable,
    lastAction,
    emissionBlockedReason:
      status.status === 'tracking' ? emissionBlockedReason(control, null, roomId) : status.detail ?? 'Tracking is not active.',
    recording,
  }

  return {
    view,
    pairs: deps.policy.pairs,
    videoRef,
    bindVideo,
    enable,
    disable,
    selectDevice,
    downloadRecording,
    clearRecording,
  }
}

function deriveStatus(
  enabled: boolean,
  camera: CameraState,
  recognizer: RecognizerStatus,
  recognizerDetail: string | null,
): { status: GestureProducerStatus; detail: string | null } {
  if (!enabled) return { status: 'disabled', detail: 'Gesture tracking is off. Enable it to start the camera.' }
  if (recognizer === 'model_failed_to_load') {
    return {
      status: 'model_failed_to_load',
      detail: `The gesture model failed to load; emission is disabled. ${recognizerDetail ?? ''}`.trim(),
    }
  }
  switch (camera.status) {
    case 'permission_denied':
      return { status: 'permission_denied', detail: camera.detail }
    case 'webcam_dropped':
      return { status: 'webcam_dropped', detail: camera.detail ?? 'The webcam dropped; emission is disabled.' }
    case 'unavailable':
      return { status: 'camera_unavailable', detail: camera.detail ?? 'No camera is available.' }
    case 'idle':
    case 'starting':
      return { status: 'starting', detail: 'Starting the camera.' }
    case 'streaming':
      return recognizer === 'ready'
        ? { status: 'tracking', detail: null }
        : { status: 'starting', detail: 'Loading the gesture model from the MediaPipe CDN.' }
  }
}

/**
 * Why an accepted gesture would emit nothing. With `pair` null, answers for any
 * draft; with a pair, answers for that pair's action. The console connection
 * feeds the roster and selection a draft is built from, so it is checked before
 * the webcam source that would carry the intent: a stale roster must never be
 * drafted against while the console channel is down.
 */
export function emissionBlockedReason(
  bindings: Pick<GestureControlBindings, 'state' | 'pendingRequest'>,
  pair: GesturePair | null,
  roomId: string,
): string | null {
  const { state, pendingRequest } = bindings
  if (state.connection.status !== 'connected') {
    return `The console connection is ${state.connection.status}; no gesture intent can be drafted.`
  }
  if (state.webcamConnection.status !== 'connected') {
    return 'The webcam relay source is not connected; no gesture intent can be sent.'
  }
  const action = pair?.action ?? { kind: 'draft' as const, name: 'capture_room' as const }
  if (action.kind === 'confirm' || action.kind === 'cancel') {
    if (!pendingRequest) return `No plan preview is pending; there is nothing to ${action.kind}.`
    if (pendingRequest.intent.source !== 'webcam') {
      return `The pending preview was drafted by ${pendingRequest.intent.source}; gestures only ${action.kind} gesture-drafted previews.`
    }
    return null
  }
  if (state.estop) {
    return 'The network stop is active; no gesture intent can be drafted until the relay reports it clear.'
  }
  if (pendingRequest) {
    return 'A plan preview is already pending; confirm or cancel it before drafting another.'
  }
  if (state.selection.length === 0) return 'Select at least one ready aircraft.'
  const notReady = state.selection.find(
    (id) => state.aircraft[id]?.membership !== 'ready' || !state.aircraft[id]?.selectable,
  )
  if (notReady !== undefined) return `${formatDroneId(notReady)} is not ready or selectable.`
  // hold, takeoff, translate, and land address the ready selection and need nothing more.
  if (action.name !== 'capture_room') return null
  if (!roomId.trim()) return 'Enter a room identifier.'
  if (state.selection.length !== 1) return 'Select exactly one ready aircraft for capture_room.'
  const selected = state.aircraft[state.selection[0]]
  if (!selected.camera_patterns.includes(state.capturePattern)) {
    return `${formatDroneId(selected.drone_id)} does not report ${state.capturePattern}; the console will not substitute a pattern.`
  }
  return null
}
