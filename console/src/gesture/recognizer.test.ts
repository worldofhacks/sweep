import { describe, expect, test, vi } from 'vitest'
import {
  HAND_CONNECTIONS,
  ModelLoadError,
  createMediaPipeGestureSource,
  normalizeGestureResult,
  type MediaPipeGestureRecognizer,
  type MediaPipeGestureResult,
  type MediaPipeVisionModule,
} from './recognizer'

function landmarks(): Array<{ x: number; y: number; z: number }> {
  return Array.from({ length: 21 }, (_, index) => ({ x: index / 21, y: 0.5, z: 0 }))
}

function readyVideo(readyState = 4): HTMLVideoElement {
  const video = document.createElement('video')
  Object.defineProperty(video, 'readyState', { value: readyState })
  Object.defineProperty(video, 'videoWidth', { value: 640 })
  Object.defineProperty(video, 'videoHeight', { value: 480 })
  return video
}

function fakeModule(
  create: MediaPipeVisionModule['GestureRecognizer']['createFromOptions'],
): MediaPipeVisionModule {
  return {
    FilesetResolver: { forVisionTasks: vi.fn(async () => ({ wasm: true })) },
    GestureRecognizer: { createFromOptions: create },
  }
}

describe('normalizeGestureResult', () => {
  test('projects built-in categories, handedness, and landmarks', () => {
    const result: MediaPipeGestureResult = {
      landmarks: [landmarks()],
      handedness: [[{ categoryName: 'Right', score: 0.99 }]],
      gestures: [[{ categoryName: 'Open_Palm', score: 0.91 }]],
    }
    const frame = normalizeGestureResult(result, 1234)
    expect(frame.t).toBe(1234)
    expect(frame.hands).toHaveLength(1)
    expect(frame.hands[0]).toMatchObject({
      category: 'Open_Palm',
      rawCategory: 'Open_Palm',
      score: 0.91,
      handedness: 'Right',
    })
    expect(frame.hands[0].landmarks).toHaveLength(21)
    expect(frame.hands[0].landmarks[1]).toEqual({ x: 1 / 21, y: 0.5, z: 0 })
  })

  test('keeps unknown labels for the recording but maps them to no category', () => {
    const frame = normalizeGestureResult(
      { landmarks: [landmarks()], handedness: [[]], gestures: [[{ categoryName: 'Rock_On', score: 0.7 }]] },
      1,
    )
    expect(frame.hands[0]).toMatchObject({ category: null, rawCategory: 'Rock_On', score: 0.7, handedness: null })
  })

  test('returns no hands for an empty result', () => {
    expect(normalizeGestureResult({ landmarks: [], handedness: [], gestures: [] }, 9)).toEqual({
      t: 9,
      hands: [],
    })
  })

  test('hand connections cover the 21-point model', () => {
    expect(HAND_CONNECTIONS).toHaveLength(21)
    expect(Math.max(...HAND_CONNECTIONS.flat())).toBe(20)
  })
})

describe('createMediaPipeGestureSource', () => {
  test('reports model_failed_to_load when the CDN module cannot be imported and allows a retry', async () => {
    let attempts = 0
    const source = createMediaPipeGestureSource({
      loadModule: async () => {
        attempts += 1
        throw new Error('cdn unreachable')
      },
    })

    await expect(source.load()).rejects.toBeInstanceOf(ModelLoadError)
    await expect(source.load()).rejects.toMatchObject({
      status: 'model_failed_to_load',
      message: expect.stringContaining('cdn unreachable'),
    })
    expect(attempts).toBe(2)
    expect(() => source.recognize(readyVideo(), 0)).toThrow(ModelLoadError)
  })

  test('reports model_failed_to_load when every delegate fails to create the recognizer', async () => {
    const create = vi.fn(async () => {
      throw new Error('model 404')
    })
    const source = createMediaPipeGestureSource({ loadModule: async () => fakeModule(create) })

    await expect(source.load()).rejects.toMatchObject({
      status: 'model_failed_to_load',
      message: expect.stringContaining('model 404'),
    })
    expect(create).toHaveBeenCalledTimes(2)
    expect(create.mock.calls.map((call) => (call as unknown[])[1])).toMatchObject([
      { baseOptions: { delegate: 'GPU' }, runningMode: 'VIDEO', numHands: 1 },
      { baseOptions: { delegate: 'CPU' }, runningMode: 'VIDEO', numHands: 1 },
    ])
  })

  test('falls back to the CPU delegate, feeds increasing timestamps, and skips unready frames', async () => {
    const recognizeForVideo = vi.fn(
      (): MediaPipeGestureResult => ({
        landmarks: [landmarks()],
        handedness: [[{ categoryName: 'Left', score: 1 }]],
        gestures: [[{ categoryName: 'Thumb_Up', score: 0.88 }]],
      }),
    )
    const recognizer: MediaPipeGestureRecognizer = { recognizeForVideo, close: vi.fn() }
    const create = vi.fn(async (_fileset: unknown, options: { baseOptions: { delegate: string } }) => {
      if (options.baseOptions.delegate === 'GPU') throw new Error('no webgl')
      return recognizer
    })
    const source = createMediaPipeGestureSource({
      loadModule: async () => fakeModule(create),
      modelUrl: 'https://example.invalid/model.task',
    })

    await source.load()
    await source.load()
    expect(create).toHaveBeenCalledTimes(2)
    expect(create.mock.calls[1][1].baseOptions).toEqual({
      modelAssetPath: 'https://example.invalid/model.task',
      delegate: 'CPU',
    })

    expect(source.recognize(readyVideo(0), 5)).toBeNull()
    expect(recognizeForVideo).not.toHaveBeenCalled()

    const video = readyVideo()
    expect(source.recognize(video, 10.6)?.hands[0]).toMatchObject({ category: 'Thumb_Up', score: 0.88 })
    source.recognize(video, 10.2)
    source.recognize(video, 10.9)
    expect(recognizeForVideo.mock.calls.map((call) => (call as unknown[])[1])).toEqual([10, 11, 12])

    source.close()
    expect(recognizer.close).toHaveBeenCalledTimes(1)
    expect(() => source.recognize(video, 20)).toThrow(ModelLoadError)
  })
})
