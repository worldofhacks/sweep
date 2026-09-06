import { describe, expect, test } from 'vitest'
import type { ConsoleIntentName } from '../relay/contract'
import {
  DEFAULT_GESTURE_PAIRS,
  DEFAULT_GESTURE_POLICY_CONFIG,
  GESTURE_CATEGORIES,
  GESTURE_EMITTABLE_NAMES,
  GESTURE_TRANSLATE_STEP,
  NEVER_GESTURE_EMITTABLE,
  createGesturePolicyState,
  describeGestureAction,
  gestureLabel,
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

/** The default pair for a gesture, so tests never depend on table order. */
function pairFor(gesture: GestureCategory): GesturePair {
  const pair = DEFAULT_GESTURE_PAIRS.find((item) => item.gesture === gesture)
  if (!pair) throw new Error(`${gesture} is not in the default pairs`)
  return pair
}

describe('gesture pairs', () => {
  test('default pairs map exactly the seven enabled gestures and pass validation', () => {
    expect(
      DEFAULT_GESTURE_PAIRS.map((pair) => [pair.gesture, pair.action, pair.minScore, pair.dwellMs]),
    ).toEqual([
      ['Open_Palm', { kind: 'draft', name: 'capture_room' }, 0.8, 600],
      ['Closed_Fist', { kind: 'draft', name: 'hold' }, 0.8, 600],
      ['Pointing_Up', { kind: 'draft', name: 'takeoff' }, 0.8, 600],
      ['Victory', { kind: 'draft', name: 'translate' }, 0.8, 600],
      ['ILoveYou', { kind: 'draft', name: 'land' }, 0.8, 600],
      ['Thumb_Up', { kind: 'confirm' }, 0.8, 400],
      ['Thumb_Down', { kind: 'cancel' }, 0.8, 400],
    ])
    expect(validateGesturePairs(DEFAULT_GESTURE_PAIRS)).toEqual([])
    expect(GESTURE_TRANSLATE_STEP).toEqual({ dx: 1, dy: 0 })
    expect([...GESTURE_EMITTABLE_NAMES]).toEqual(['capture_room', 'hold', 'takeoff', 'translate', 'land'])
  })

  test('estop, arm, disarm, land_all, come_home, altitude, formation, spacing, sweep and select are never gesture-emittable', () => {
    expect([...NEVER_GESTURE_EMITTABLE]).toEqual([
      'estop',
      'arm',
      'disarm',
      'land_all',
      'come_home',
      'altitude',
      'formation_next',
      'formation_set',
      'spacing',
      'sweep',
      'select',
    ])
    for (const name of NEVER_GESTURE_EMITTABLE) {
      expect(isGestureEmittable(name)).toBe(false)
      expect(GESTURE_EMITTABLE_NAMES.has(name as ConsoleIntentName)).toBe(false)
      const pair = {
        gesture: 'Victory',
        action: { kind: 'draft', name },
        minScore: 0.8,
        dwellMs: 600,
      } as unknown as GesturePair
      expect(validateGesturePairs([pair])).toEqual([`${name} is never gesture-emittable.`])
    }
    for (const name of ['capture_room', 'hold', 'takeoff', 'translate', 'land']) {
      expect(isGestureEmittable(name)).toBe(true)
    }
    const offList = {
      gesture: 'Victory',
      action: { kind: 'draft', name: 'survey_area' },
      minScore: 0.8,
      dwellMs: 600,
    } as unknown as GesturePair
    expect(validateGesturePairs([offList])).toEqual(['survey_area is not on the gesture-emittable allowlist.'])
  })

  test('gesture labels read the way the design names poses', () => {
    expect(GESTURE_CATEGORIES.map(gestureLabel)).toEqual([
      'None',
      'Closed fist',
      'Open palm',
      'Pointing up',
      'Thumb down',
      'Thumb up',
      'Victory',
      'I love you',
    ])
    expect(describeGestureAction(pairFor('Victory').action)).toBe('draft translate forward one step')
    expect(describeGestureAction(pairFor('Pointing_Up').action)).toBe('draft takeoff')
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
    const withoutVictory: GesturePolicyConfig = {
      ...DEFAULT_GESTURE_POLICY_CONFIG,
      pairs: DEFAULT_GESTURE_PAIRS.filter((pair) => pair.gesture !== 'Victory'),
    }
    const { outcomes, state } = run(held('Victory', 0, 1000), withoutVictory)
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
    expect(accepted[1]).toMatchObject({ pair: pairFor('Thumb_Up'), heldMs: 400 })
    expect(outcomes[20]).toMatchObject({ kind: 'duplicate_suppressed', category: 'Thumb_Up' })
    expect(outcomes[27]).toMatchObject({ kind: 'duplicate_suppressed', category: 'Victory' })
  })

  describe.each([
    ['Pointing_Up', 'takeoff'],
    ['Victory', 'translate'],
    ['ILoveYou', 'land'],
  ] as const)('%s drafts %s', (gesture, name) => {
    test('is accepted once after the 600 ms draft dwell at score 0.8, then suppressed until neutral', () => {
      const pair = pairFor(gesture)
      expect(pair).toMatchObject({ action: { kind: 'draft', name }, minScore: 0.8, dwellMs: 600 })
      const { outcomes, state } = run([...held(gesture, 0, 600, 0.8), ...held(gesture, 650, 900), ...held(null, 950, 1150)])
      expect(outcomes[0]).toEqual({ kind: 'candidate', pair, heldMs: 0, progress: 0 })
      expect(outcomes[11]).toMatchObject({ kind: 'candidate', heldMs: 550 })
      expect(outcomes[12]).toEqual({ kind: 'accepted', pair, heldMs: 600 })
      expect(kinds(outcomes).filter((kind) => kind === 'accepted')).toHaveLength(1)
      expect(kinds(outcomes.slice(13, 19))).toEqual(Array(6).fill('duplicate_suppressed'))
      expect(outcomes[13]).toMatchObject({ kind: 'duplicate_suppressed', pair, category: gesture })
      expect(outcomes.at(-1)).toEqual({ kind: 'released', pair })
      expect(state.phase).toBe('idle')
    })

    test('low confidence never starts a candidate and abandons one in progress', () => {
      const pair = pairFor(gesture)
      const idle = run([frame(0, gesture, 0.79)])
      expect(idle.outcomes).toEqual([{ kind: 'low_confidence', pair, score: 0.79 }])
      expect(idle.state.phase).toBe('idle')

      const dip = run([...held(gesture, 0, 300), frame(350, gesture, 0.5), ...held(gesture, 400, 950)])
      expect(dip.outcomes[7]).toEqual({ kind: 'low_confidence', pair, score: 0.5 })
      expect(dip.outcomes[8]).toMatchObject({ kind: 'candidate', heldMs: 0 })
      expect(kinds(dip.outcomes)).not.toContain('accepted')
    })

    test('releasing, changing gesture, or a frame gap before the dwell is a timeout that emits nothing', () => {
      const pair = pairFor(gesture)
      const released = run([...held(gesture, 0, 300), frame(350, null)])
      expect(released.outcomes.at(-1)).toEqual({ kind: 'dwell_timeout', pair, heldMs: 350, reason: 'released' })
      expect(released.state.phase).toBe('idle')

      const changed = run([...held(gesture, 0, 300), frame(350, 'Open_Palm')])
      expect(changed.outcomes.at(-1)).toMatchObject({ kind: 'dwell_timeout', pair, heldMs: 350, reason: 'gesture_changed' })

      const gap = run([frame(0, gesture), frame(100, gesture), frame(900, gesture)])
      expect(gap.outcomes[2]).toEqual({ kind: 'dwell_timeout', pair, heldMs: 100, reason: 'frame_gap' })
      expect(gap.state.phase).toBe('idle')
      for (const { outcomes } of [released, changed, gap]) expect(kinds(outcomes)).not.toContain('accepted')
    })

    test('a held pose drafts once and a repeat before neutral is a suppressed duplicate', () => {
      const pair = pairFor(gesture)
      const { outcomes } = run([...held(gesture, 0, 1500), frame(1550, null), frame(1600, 'None'), ...held(gesture, 1650, 2300)])
      expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toEqual([{ kind: 'accepted', pair, heldMs: 600 }])
      expect(outcomes[13]).toMatchObject({ kind: 'duplicate_suppressed', pair, category: gesture })
      expect(outcomes[31]).toMatchObject({ kind: 'wait_for_release', neutralMs: 0 })
      expect(outcomes[32]).toMatchObject({ kind: 'wait_for_release', neutralMs: 50 })
      expect(outcomes[33]).toMatchObject({ kind: 'duplicate_suppressed', pair, category: gesture })
    })
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
