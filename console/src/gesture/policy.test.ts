import { describe, expect, test } from 'vitest'
import {
  DEFAULT_GESTURE_PAIRS,
  DEFAULT_GESTURE_POLICY_CONFIG,
  NEVER_GESTURE_EMITTABLE,
  createGesturePolicyState,
  isGestureEmittable,
  stepGesturePolicy,
  validateGesturePairs,
  type GestureCategory,
  type GestureObservation,
  type GesturePair,
  type GesturePolicyConfig,
  type GesturePolicyOutcome,
  type GesturePolicyState,
} from './policy'

const FRAME_MS = 50

function frame(t: number, category: GestureCategory | null, score = 0.95): GestureObservation {
  return { t, category, score }
}

/** Runs a scripted sequence of observations and returns every outcome in order. */
function run(
  observations: GestureObservation[],
  config: GesturePolicyConfig = DEFAULT_GESTURE_POLICY_CONFIG,
  initial: GesturePolicyState = createGesturePolicyState(),
): { outcomes: GesturePolicyOutcome[]; state: GesturePolicyState } {
  let state = initial
  const outcomes: GesturePolicyOutcome[] = []
  observations.forEach((observation) => {
    const step = stepGesturePolicy(state, observation, config)
    state = step.state
    outcomes.push(step.outcome)
  })
  return { outcomes, state }
}

/** Frames every FRAME_MS from `from` to `to` inclusive. */
function held(
  category: GestureCategory | null,
  from: number,
  to: number,
  score = 0.95,
): GestureObservation[] {
  const frames: GestureObservation[] = []
  for (let t = from; t <= to; t += FRAME_MS) frames.push(frame(t, category, score))
  return frames
}

const kinds = (outcomes: GesturePolicyOutcome[]) => outcomes.map((outcome) => outcome.kind)

describe('gesture pairs', () => {
  test('default pairs map exactly the four enabled gestures and pass validation', () => {
    expect(
      DEFAULT_GESTURE_PAIRS.map((pair) => [pair.gesture, pair.action, pair.minScore, pair.dwellMs]),
    ).toEqual([
      ['Open_Palm', { kind: 'draft', name: 'capture_room' }, 0.8, 600],
      ['Closed_Fist', { kind: 'draft', name: 'hold' }, 0.8, 600],
      ['Thumb_Up', { kind: 'confirm' }, 0.8, 400],
      ['Thumb_Down', { kind: 'cancel' }, 0.8, 400],
    ])
    expect(validateGesturePairs(DEFAULT_GESTURE_PAIRS)).toEqual([])
  })

  test('estop, arm, takeoff, and free-flight motion are never gesture-emittable', () => {
    for (const name of ['estop', 'arm', 'takeoff', 'translate', 'altitude', 'come_home', 'land_all']) {
      expect(NEVER_GESTURE_EMITTABLE).toContain(name)
      expect(isGestureEmittable(name)).toBe(false)
      const pair = {
        gesture: 'Victory',
        action: { kind: 'draft', name },
        minScore: 0.8,
        dwellMs: 600,
      } as unknown as GesturePair
      expect(validateGesturePairs([pair])).toEqual([`${name} is never gesture-emittable.`])
    }
    expect(isGestureEmittable('capture_room')).toBe(true)
    expect(isGestureEmittable('hold')).toBe(true)
  })

  test('rejects duplicate, neutral, and malformed pairs', () => {
    const problems = validateGesturePairs([
      { gesture: 'None', action: { kind: 'confirm' }, minScore: 0.8, dwellMs: 400 },
      { gesture: 'Thumb_Up', action: { kind: 'confirm' }, minScore: 1.2, dwellMs: 0 },
      { gesture: 'Thumb_Up', action: { kind: 'cancel' }, minScore: 0.8, dwellMs: 400 },
    ])
    expect(problems).toEqual([
      'None cannot be mapped; it is the neutral release state.',
      'Thumb_Up needs a score threshold in (0, 1].',
      'Thumb_Up needs a positive dwell.',
      'Thumb_Up is mapped more than once.',
    ])
  })
})

describe('gesture policy state machine', () => {
  test('idle -> candidate -> accepted once after the draft dwell, then suppressed until release', () => {
    const { outcomes, state } = run([
      ...held('Open_Palm', 0, 600),
      ...held('Open_Palm', 650, 900),
      ...held(null, 950, 1100),
      ...held('None', 1150, 1200),
    ])

    expect(outcomes[0]).toEqual({
      kind: 'candidate',
      pair: DEFAULT_GESTURE_PAIRS[0],
      heldMs: 0,
      progress: 0,
    })
    expect(outcomes[6]).toMatchObject({ kind: 'candidate', heldMs: 300, progress: 0.5 })
    expect(outcomes[12]).toEqual({ kind: 'accepted', pair: DEFAULT_GESTURE_PAIRS[0], heldMs: 600 })
    expect(kinds(outcomes).filter((kind) => kind === 'accepted')).toHaveLength(1)
    expect(kinds(outcomes.slice(13, 19))).toEqual(Array(6).fill('duplicate_suppressed'))
    expect(outcomes[13]).toMatchObject({ category: 'Open_Palm', pair: DEFAULT_GESTURE_PAIRS[0] })
    expect(kinds(outcomes.slice(19))).toEqual([
      'wait_for_release',
      'wait_for_release',
      'wait_for_release',
      'wait_for_release',
      'released',
      'idle',
    ])
    expect(state.phase).toBe('idle')
  })

  test('walks every phase in order and can be accepted again after a clean release', () => {
    const phases: string[] = []
    let state = createGesturePolicyState()
    const script = [
      ...held('Closed_Fist', 0, 600),
      ...held(null, 650, 850),
      ...held('Closed_Fist', 900, 1500),
    ]
    let accepted = 0
    script.forEach((observation) => {
      const step = stepGesturePolicy(state, observation)
      if (step.state.phase !== phases[phases.length - 1]) phases.push(step.state.phase)
      if (step.outcome.kind === 'accepted') accepted += 1
      state = step.state
    })
    expect(phases).toEqual([
      'candidate',
      'accepted',
      'wait_for_release',
      'idle',
      'candidate',
      'accepted',
    ])
    expect(accepted).toBe(2)
  })

  test('confirm and cancel use the shorter decision dwell', () => {
    const thumbUp = run(held('Thumb_Up', 0, 400))
    expect(thumbUp.outcomes.at(-2)).toMatchObject({ kind: 'candidate', heldMs: 350 })
    expect(thumbUp.outcomes.at(-1)).toMatchObject({ kind: 'accepted', heldMs: 400 })

    const thumbDown = run(held('Thumb_Down', 0, 350))
    expect(kinds(thumbDown.outcomes)).not.toContain('accepted')
    expect(thumbDown.state.phase).toBe('candidate')
  })

  test('low confidence never starts a candidate and abandons one in progress', () => {
    const idle = run([frame(0, 'Open_Palm', 0.79)])
    expect(idle.outcomes).toEqual([
      { kind: 'low_confidence', pair: DEFAULT_GESTURE_PAIRS[0], score: 0.79 },
    ])
    expect(idle.state.phase).toBe('idle')

    const dip = run([
      ...held('Open_Palm', 0, 300),
      frame(350, 'Open_Palm', 0.5),
      ...held('Open_Palm', 400, 950),
    ])
    expect(dip.outcomes[7]).toEqual({
      kind: 'low_confidence',
      pair: DEFAULT_GESTURE_PAIRS[0],
      score: 0.5,
    })
    expect(dip.outcomes[8]).toMatchObject({ kind: 'candidate', heldMs: 0 })
    expect(dip.outcomes.at(-1)).toMatchObject({ kind: 'candidate', heldMs: 550 })
    expect(kinds(dip.outcomes)).not.toContain('accepted')
  })

  test('releasing before the dwell is a dwell timeout that emits nothing', () => {
    const { outcomes, state } = run([...held('Open_Palm', 0, 300), frame(350, null)])
    expect(outcomes.at(-1)).toEqual({
      kind: 'dwell_timeout',
      pair: DEFAULT_GESTURE_PAIRS[0],
      heldMs: 350,
      reason: 'released',
    })
    expect(kinds(outcomes)).not.toContain('accepted')
    expect(state.phase).toBe('idle')
  })

  test('changing gesture mid-dwell is a dwell timeout and restarts on the new gesture', () => {
    const { outcomes } = run([...held('Open_Palm', 0, 300), ...held('Closed_Fist', 350, 400)])
    expect(outcomes[7]).toMatchObject({
      kind: 'dwell_timeout',
      reason: 'gesture_changed',
      heldMs: 350,
    })
    expect(outcomes[8]).toMatchObject({
      kind: 'candidate',
      pair: DEFAULT_GESTURE_PAIRS[1],
      heldMs: 0,
    })
  })

  test('a frame gap longer than the limit cannot prove dwell even when wall time passed', () => {
    const { outcomes, state } = run([
      frame(0, 'Open_Palm'),
      frame(100, 'Open_Palm'),
      frame(900, 'Open_Palm'),
    ])
    expect(outcomes[2]).toEqual({
      kind: 'dwell_timeout',
      pair: DEFAULT_GESTURE_PAIRS[0],
      heldMs: 100,
      reason: 'frame_gap',
    })
    expect(state.phase).toBe('idle')
  })

  test('unmapped gestures are reported and never build a candidate', () => {
    const { outcomes, state } = run(held('Victory', 0, 1000))
    expect(new Set(kinds(outcomes))).toEqual(new Set(['unmapped']))
    expect(outcomes[0]).toEqual({ kind: 'unmapped', category: 'Victory' })
    expect(state.phase).toBe('idle')
  })

  test('switching straight from an accepted gesture to another is suppressed until neutral', () => {
    const { outcomes } = run([
      ...held('Open_Palm', 0, 600),
      ...held('Thumb_Up', 650, 1300),
      ...held('Victory', 1350, 1400),
      ...held(null, 1450, 1650),
      ...held('Thumb_Up', 1700, 2100),
    ])
    const accepted = outcomes.filter((outcome) => outcome.kind === 'accepted')
    expect(accepted).toHaveLength(2)
    expect(accepted[0]).toMatchObject({ pair: DEFAULT_GESTURE_PAIRS[0] })
    expect(accepted[1]).toMatchObject({ pair: DEFAULT_GESTURE_PAIRS[2], heldMs: 400 })
    expect(outcomes[20]).toMatchObject({ kind: 'duplicate_suppressed', category: 'Thumb_Up' })
    expect(outcomes[27]).toMatchObject({ kind: 'duplicate_suppressed', category: 'Victory' })
  })

  test('a neutral flicker shorter than the release window does not re-arm', () => {
    const { outcomes } = run([
      ...held('Open_Palm', 0, 600),
      frame(650, null),
      frame(700, 'None'),
      ...held('Open_Palm', 750, 1400),
    ])
    expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toHaveLength(1)
    expect(outcomes[13]).toMatchObject({ kind: 'wait_for_release', neutralMs: 0 })
    expect(outcomes[14]).toMatchObject({ kind: 'wait_for_release', neutralMs: 50 })
    expect(outcomes[15]).toMatchObject({ kind: 'duplicate_suppressed', category: 'Open_Palm' })
  })

  test('thresholds and dwell are adjustable per pair', () => {
    const config: GesturePolicyConfig = {
      pairs: [
        { gesture: 'Victory', action: { kind: 'draft', name: 'hold' }, minScore: 0.5, dwellMs: 100 },
      ],
      releaseMs: 0,
      maxFrameGapMs: 1000,
    }
    expect(validateGesturePairs(config.pairs)).toEqual([])
    const { outcomes } = run(
      [frame(0, 'Victory', 0.6), frame(100, 'Victory', 0.6), frame(200, null)],
      config,
    )
    expect(kinds(outcomes)).toEqual(['candidate', 'accepted', 'released'])
  })
})
