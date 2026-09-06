import { describe, expect, test } from 'vitest'
import { DEFAULT_GESTURE_PAIRS } from './policy'
import { createSessionRecorder, summarizeOutcome } from './recorder'

const wall = 1_756_700_000_000

describe('session recorder', () => {
  test('writes a header line then sequenced entries with monotonic and wall time', () => {
    let now = wall
    const recorder = createSessionRecorder({
      sessionId: 'session-rec',
      pairs: DEFAULT_GESTURE_PAIRS,
      wallClock: () => now,
    })
    recorder.record({
      kind: 'recognizer',
      t: 10.123456,
      hands: [
        {
          category: 'Open_Palm',
          rawCategory: 'Open_Palm',
          score: 0.912345,
          handedness: 'Right',
          landmarks: [{ x: 0.123456, y: 0.5, z: -0.000049 }],
        },
      ],
    })
    now += 40
    recorder.record({
      kind: 'policy',
      t: 50,
      phase: 'candidate',
      outcome: { kind: 'candidate', pair: DEFAULT_GESTURE_PAIRS[0], heldMs: 40, progress: 40 / 600 },
    })
    recorder.record({
      kind: 'intent',
      t: 60,
      event: 'draft',
      intent_id: 'intent-1',
      name: 'capture_room',
      detail: null,
    })
    recorder.record({
      kind: 'status',
      t: 70,
      enabled: true,
      camera: 'webcam_dropped',
      recognizer: 'ready',
      detail: 'The camera track ended.',
    })

    const lines = recorder.toJsonl().trimEnd().split('\n').map((line) => JSON.parse(line))
    expect(lines).toHaveLength(5)
    expect(lines[0]).toEqual({
      kind: 'header',
      v: 1,
      session: 'session-rec',
      source: 'webcam',
      wall_t: now,
      pairs: [
        { gesture: 'Open_Palm', action: 'draft:capture_room', minScore: 0.8, dwellMs: 600 },
        { gesture: 'Closed_Fist', action: 'draft:hold', minScore: 0.8, dwellMs: 600 },
        { gesture: 'Pointing_Up', action: 'draft:takeoff', minScore: 0.8, dwellMs: 600 },
        { gesture: 'Victory', action: 'draft:translate', minScore: 0.8, dwellMs: 600 },
        { gesture: 'ILoveYou', action: 'draft:land', minScore: 0.8, dwellMs: 600 },
        { gesture: 'Thumb_Up', action: 'confirm', minScore: 0.8, dwellMs: 400 },
        { gesture: 'Thumb_Down', action: 'cancel', minScore: 0.8, dwellMs: 400 },
      ],
    })
    expect(lines[1]).toEqual({
      seq: 1,
      t: 10.123456,
      wall_t: wall,
      kind: 'recognizer',
      hands: [
        {
          category: 'Open_Palm',
          rawCategory: 'Open_Palm',
          score: 0.9123,
          handedness: 'Right',
          landmarks: [{ x: 0.1235, y: 0.5, z: 0 }],
        },
      ],
    })
    expect(lines[2]).toEqual({
      seq: 2,
      t: 50,
      wall_t: wall + 40,
      kind: 'policy',
      phase: 'candidate',
      outcome: { kind: 'candidate', gesture: 'Open_Palm', action: 'draft:capture_room', heldMs: 40, progress: 0.0667 },
    })
    expect(lines[3]).toMatchObject({ seq: 3, kind: 'intent', event: 'draft', intent_id: 'intent-1' })
    expect(lines[4]).toMatchObject({ seq: 4, kind: 'status', camera: 'webcam_dropped' })
    expect(recorder.size).toBe(4)
  })

  test('drops the oldest entries past capacity and reports the count', () => {
    const recorder = createSessionRecorder({
      sessionId: 's',
      pairs: [],
      wallClock: () => wall,
      capacity: 2,
    })
    for (let index = 0; index < 5; index += 1) {
      recorder.record({ kind: 'policy', t: index, phase: 'idle', outcome: { kind: 'idle' } })
    }
    expect(recorder.size).toBe(2)
    expect(recorder.dropped).toBe(3)
    expect(recorder.entries().map((entry) => entry.seq)).toEqual([4, 5])

    recorder.clear()
    expect(recorder.size).toBe(0)
    expect(recorder.dropped).toBe(0)
    expect(recorder.toJsonl().trimEnd().split('\n')).toHaveLength(1)
  })

  test('summarizes every outcome kind without the nested pair object', () => {
    const pair = DEFAULT_GESTURE_PAIRS.find((item) => item.gesture === 'Thumb_Up') as (typeof DEFAULT_GESTURE_PAIRS)[number]
    expect(summarizeOutcome({ kind: 'idle' })).toEqual({ kind: 'idle' })
    expect(summarizeOutcome({ kind: 'unmapped', category: 'Victory' })).toEqual({ kind: 'unmapped', category: 'Victory' })
    expect(summarizeOutcome({ kind: 'low_confidence', pair, score: 0.51 })).toEqual({
      kind: 'low_confidence',
      gesture: 'Thumb_Up',
      action: 'confirm',
      score: 0.51,
    })
    expect(summarizeOutcome({ kind: 'accepted', pair, heldMs: 400 })).toEqual({
      kind: 'accepted',
      gesture: 'Thumb_Up',
      action: 'confirm',
      heldMs: 400,
    })
    expect(summarizeOutcome({ kind: 'dwell_timeout', pair, heldMs: 100, reason: 'released' })).toEqual({
      kind: 'dwell_timeout',
      gesture: 'Thumb_Up',
      action: 'confirm',
      heldMs: 100,
      reason: 'released',
    })
    expect(summarizeOutcome({ kind: 'duplicate_suppressed', pair, category: 'Thumb_Up' })).toEqual({
      kind: 'duplicate_suppressed',
      gesture: 'Thumb_Up',
      action: 'confirm',
      category: 'Thumb_Up',
    })
    expect(summarizeOutcome({ kind: 'wait_for_release', pair, neutralMs: 50 })).toEqual({
      kind: 'wait_for_release',
      gesture: 'Thumb_Up',
      action: 'confirm',
      neutralMs: 50,
    })
    expect(summarizeOutcome({ kind: 'released', pair })).toEqual({ kind: 'released', gesture: 'Thumb_Up', action: 'confirm' })
  })
})
