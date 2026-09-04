/**
 * Thin adapter over @mediapipe/tasks-vision GestureRecognizer behind the
 * GestureSource interface. The MediaPipe module is imported lazily and the
 * WASM runtime and model load from the MediaPipe CDN at runtime; a failure to
 * load is reported as the `model_failed_to_load` state, never thrown into the
 * frame loop. Tests inject scripted frames through the same interface.
 */
import { GESTURE_CATEGORIES, type GestureCategory } from './policy'

export interface HandLandmark {
  x: number
  y: number
  z: number
}

export interface HandObservation {
  /** Built-in gesture class for this hand, or null when the classifier returned nothing usable. */
  category: GestureCategory | null
  /** Raw classifier label, kept for the session recording even when unmapped. */
  rawCategory: string | null
  score: number
  handedness: string | null
  landmarks: HandLandmark[]
}

export interface GestureFrame {
  /** Monotonic milliseconds at which the frame was recognized. */
  t: number
  hands: HandObservation[]
}

export type RecognizerStatus = 'unloaded' | 'loading' | 'ready' | 'model_failed_to_load'

export interface GestureSource {
  /** Resolves once frames can be recognized; rejects with ModelLoadError otherwise. */
  load(): Promise<void>
  /** Recognizes one video frame, or returns null when the frame is not usable yet. */
  recognize(video: HTMLVideoElement, t: number): GestureFrame | null
  close(): void
}

export class ModelLoadError extends Error {
  readonly status = 'model_failed_to_load' as const

  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'ModelLoadError'
  }
}

export const MEDIAPIPE_TASKS_VISION_VERSION = '1.0.1'
export const MEDIAPIPE_VISION_WASM_URL = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MEDIAPIPE_TASKS_VISION_VERSION}/wasm`
export const GESTURE_RECOGNIZER_MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task'

/** Landmark index pairs for the 21-point MediaPipe hand model, used by the overlay. */
export const HAND_CONNECTIONS: ReadonlyArray<readonly [number, number]> = Object.freeze([
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20],
  [0, 17],
])

/** The subset of the MediaPipe module this adapter touches. */
export interface MediaPipeVisionModule {
  FilesetResolver: {
    forVisionTasks(basePath?: string): Promise<unknown>
  }
  GestureRecognizer: {
    createFromOptions(
      fileset: unknown,
      options: {
        baseOptions: { modelAssetPath: string; delegate: 'CPU' | 'GPU' }
        runningMode: 'VIDEO'
        numHands: number
      },
    ): Promise<MediaPipeGestureRecognizer>
  }
}

export interface MediaPipeGestureRecognizer {
  recognizeForVideo(video: HTMLVideoElement, timestampMs: number): MediaPipeGestureResult
  close(): void
}

export interface MediaPipeGestureResult {
  landmarks: Array<Array<{ x: number; y: number; z: number }>>
  handedness: Array<Array<{ categoryName: string; score: number }>>
  gestures: Array<Array<{ categoryName: string; score: number }>>
}

export interface MediaPipeGestureSourceOptions {
  loadModule?: () => Promise<MediaPipeVisionModule>
  wasmUrl?: string
  modelUrl?: string
  /** Delegates to try in order; the first that initializes wins. */
  delegates?: ReadonlyArray<'GPU' | 'CPU'>
}

const HAVE_CURRENT_DATA = 2

class MediaPipeGestureSource implements GestureSource {
  private recognizer: MediaPipeGestureRecognizer | null = null
  private loading: Promise<void> | null = null
  private lastTimestamp = -1
  private readonly options: Required<MediaPipeGestureSourceOptions>

  constructor(options: MediaPipeGestureSourceOptions) {
    this.options = {
      loadModule: options.loadModule ?? (() => import('@mediapipe/tasks-vision')),
      wasmUrl: options.wasmUrl ?? MEDIAPIPE_VISION_WASM_URL,
      modelUrl: options.modelUrl ?? GESTURE_RECOGNIZER_MODEL_URL,
      delegates: options.delegates ?? ['GPU', 'CPU'],
    }
  }

  load(): Promise<void> {
    if (this.recognizer) return Promise.resolve()
    if (!this.loading) {
      this.loading = this.createRecognizer().catch((error: unknown) => {
        this.loading = null
        throw error
      })
    }
    return this.loading
  }

  recognize(video: HTMLVideoElement, t: number): GestureFrame | null {
    if (!this.recognizer) throw new ModelLoadError('Gesture model is not loaded.')
    if (video.readyState < HAVE_CURRENT_DATA || video.videoWidth === 0 || video.videoHeight === 0) {
      return null
    }
    // MediaPipe requires strictly increasing timestamps per recognizer instance.
    const timestamp = Math.max(Math.floor(t), this.lastTimestamp + 1)
    this.lastTimestamp = timestamp
    return normalizeGestureResult(this.recognizer.recognizeForVideo(video, timestamp), t)
  }

  close(): void {
    this.recognizer?.close()
    this.recognizer = null
    this.loading = null
    this.lastTimestamp = -1
  }

  private async createRecognizer(): Promise<void> {
    let module: MediaPipeVisionModule
    let fileset: unknown
    try {
      module = await this.options.loadModule()
      fileset = await module.FilesetResolver.forVisionTasks(this.options.wasmUrl)
    } catch (error) {
      throw new ModelLoadError(
        `MediaPipe runtime failed to load from ${this.options.wasmUrl}: ${describeError(error)}`,
        { cause: error },
      )
    }
    let lastError: unknown = null
    for (const delegate of this.options.delegates) {
      try {
        this.recognizer = await module.GestureRecognizer.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: this.options.modelUrl, delegate },
          runningMode: 'VIDEO',
          numHands: 1,
        })
        return
      } catch (error) {
        lastError = error
      }
    }
    throw new ModelLoadError(
      `Gesture model failed to load from ${this.options.modelUrl}: ${describeError(lastError)}`,
      { cause: lastError },
    )
  }
}

export function createMediaPipeGestureSource(options: MediaPipeGestureSourceOptions = {}): GestureSource {
  return new MediaPipeGestureSource(options)
}

/** Projects a raw MediaPipe result onto the console's closed observation shape. */
export function normalizeGestureResult(result: MediaPipeGestureResult, t: number): GestureFrame {
  const hands = result.landmarks.map((landmarks, index) => {
    const gesture = result.gestures[index]?.[0]
    const hand = result.handedness[index]?.[0]
    const rawCategory = gesture?.categoryName ?? null
    return {
      category: isGestureCategory(rawCategory) ? rawCategory : null,
      rawCategory,
      score: gesture?.score ?? 0,
      handedness: hand?.categoryName ?? null,
      landmarks: landmarks.map(({ x, y, z }) => ({ x, y, z })),
    }
  })
  return { t, hands }
}

export function isGestureCategory(value: unknown): value is GestureCategory {
  return typeof value === 'string' && (GESTURE_CATEGORIES as readonly string[]).includes(value)
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message
  return typeof error === 'string' ? error : 'unknown error'
}
