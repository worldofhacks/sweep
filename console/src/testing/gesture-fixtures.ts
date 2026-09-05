/**
 * Deterministic stand-ins for the gesture producer's browser dependencies:
 * a scripted GestureSource, a camera controller over fake media APIs, a manual
 * frame scheduler, and a controllable clock. No timers, no real media.
 */
import { createCameraController, type CameraController, type CameraDependencies } from '../gesture/camera'
import { DEFAULT_GESTURE_POLICY_CONFIG, type GestureCategory } from '../gesture/policy'
import {
  ModelLoadError,
  type GestureFrame,
  type GestureSource,
  type HandObservation,
} from '../gesture/recognizer'
import type { GestureProducerDependencies } from '../gesture/use-gesture-producer'

export function hand(category: GestureCategory | null, score = 0.95): HandObservation {
  return {
    category,
    rawCategory: category,
    score,
    handedness: 'Right',
    landmarks: Array.from({ length: 21 }, (_, index) => ({
      x: 0.3 + index * 0.01,
      y: 0.4 + (index % 5) * 0.02,
      z: 0,
    })),
  }
}

export class ScriptedGestureSource implements GestureSource {
  readonly recognized: number[] = []
  closed = false
  loadCalls = 0
  private readonly queue: Array<HandObservation[]> = []
  private readonly loadError: Error | null

  constructor(options: { loadError?: Error } = {}) {
    this.loadError = options.loadError ?? null
  }

  /** Queues the hands for the next recognized frame; no hands means no hand present. */
  script(hands: HandObservation[]): void {
    this.queue.push(hands)
  }

  load(): Promise<void> {
    this.loadCalls += 1
    if (this.loadError) return Promise.reject(new ModelLoadError(this.loadError.message))
    return Promise.resolve()
  }

  recognize(_video: HTMLVideoElement, t: number): GestureFrame | null {
    this.recognized.push(t)
    const hands = this.queue.shift() ?? []
    return { t, hands }
  }

  close(): void {
    this.closed = true
  }
}

export interface FakeMediaTrack {
  kind: 'video'
  readyState: 'live' | 'ended'
  stop(): void
  getSettings(): { deviceId: string }
  addEventListener(type: string, listener: () => void): void
  removeEventListener(type: string, listener: () => void): void
  end(): void
}

export interface FakeCamera {
  controller: CameraController
  dependencies: CameraDependencies
  /** Simulates the active track ending (device unplugged). */
  dropWebcam(): void
  denyPermission(): void
  startCalls: number
}

export function createFakeCamera(): FakeCamera {
  let track: FakeMediaTrack | null = null
  let deny = false
  const fake: FakeCamera = {
    controller: undefined as unknown as CameraController,
    dependencies: undefined as unknown as CameraDependencies,
    dropWebcam: () => track?.end(),
    denyPermission: () => {
      deny = true
    },
    startCalls: 0,
  }
  const dependencies: CameraDependencies = {
    getUserMedia: async () => {
      fake.startCalls += 1
      if (deny) throw new DOMException('Permission denied', 'NotAllowedError')
      const listeners = new Set<() => void>()
      const created: FakeMediaTrack = {
        kind: 'video',
        readyState: 'live',
        stop: () => {
          created.readyState = 'ended'
        },
        getSettings: () => ({ deviceId: 'fixture-cam' }),
        addEventListener: (_type, listener) => listeners.add(listener),
        removeEventListener: (_type, listener) => listeners.delete(listener),
        end: () => listeners.forEach((listener) => listener()),
      }
      track = created
      return {
        getTracks: () => [created],
        getVideoTracks: () => [created],
      } as unknown as MediaStream
    },
    enumerateDevices: async () => [
      { deviceId: 'fixture-cam', label: 'Fixture camera', kind: 'videoinput', groupId: 'g', toJSON: () => ({}) },
    ],
    onDeviceChange: () => () => {},
  }
  fake.dependencies = dependencies
  fake.controller = createCameraController(dependencies)
  return fake
}

export interface ManualFrameScheduler {
  scheduleFrame: GestureProducerDependencies['scheduleFrame']
  /** Runs the pending frame callback once, if any. */
  tick(): boolean
  readonly pending: boolean
}

export function createManualFrameScheduler(): ManualFrameScheduler {
  let callback: (() => void) | null = null
  return {
    scheduleFrame: (_video, next) => {
      callback = next
      return () => {
        if (callback === next) callback = null
      }
    },
    tick: () => {
      const current = callback
      callback = null
      if (!current) return false
      current()
      return true
    },
    get pending() {
      return callback !== null
    },
  }
}

export interface GestureTestRig {
  dependencies: GestureProducerDependencies
  source: ScriptedGestureSource
  camera: FakeCamera
  scheduler: ManualFrameScheduler
  downloads: Array<{ name: string; contents: string }>
  /** Advances the monotonic clock by `ms`, queues one frame, and runs it. */
  frame(hands: HandObservation[], ms?: number): void
  /** Feeds a held gesture for `durationMs` at `stepMs` intervals (inclusive of the end). */
  hold(category: GestureCategory | null, durationMs: number, score?: number, stepMs?: number): void
  now(): number
}

export function createGestureTestRig(options: { loadError?: Error; wallStart?: number } = {}): GestureTestRig {
  let monotonic = 0
  const wallStart = options.wallStart ?? 1_756_700_000_000
  const source = new ScriptedGestureSource({ loadError: options.loadError })
  const camera = createFakeCamera()
  const scheduler = createManualFrameScheduler()
  const downloads: Array<{ name: string; contents: string }> = []
  const dependencies: GestureProducerDependencies = {
    camera: camera.controller,
    createSource: () => source,
    clock: { monotonic: () => monotonic, wall: () => wallStart + monotonic },
    scheduleFrame: scheduler.scheduleFrame,
    attachStream: async () => {},
    downloadFile: (name, contents) => downloads.push({ name, contents }),
    policy: DEFAULT_GESTURE_POLICY_CONFIG,
  }
  const frame = (hands: HandObservation[], ms = 50) => {
    monotonic += ms
    source.script(hands)
    scheduler.tick()
  }
  return {
    dependencies,
    source,
    camera,
    scheduler,
    downloads,
    frame,
    hold: (category, durationMs, score = 0.95, stepMs = 50) => {
      for (let elapsed = 0; elapsed < durationMs; elapsed += stepMs) {
        frame(category === null ? [] : [hand(category, score)], stepMs)
      }
    },
    now: () => monotonic,
  }
}
