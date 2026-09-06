import { describe, expect, test } from 'vitest'
import type { ConsoleIntentName } from '../relay/contract'
import {
  CONSENSUS_MIN_FRAMES,
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
  observeHand,
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

const accepted = (outcomes: GesturePolicyOutcome[]) => outcomes.filter((outcome) => outcome.kind === 'accepted')

/** The default pair for a gesture, so tests never depend on table order. */
function pairFor(gesture: GestureCategory): GesturePair {
  const pair = DEFAULT_GESTURE_PAIRS.find((item) => item.gesture === gesture)
  if (!pair) throw new Error(`${gesture} is not in the default pairs`)
  return pair
}

describe('gesture pairs', () => {
  test('default pairs map exactly the seven enabled gestures with per-pose thresholds and pass validation', () => {
    expect(
      DEFAULT_GESTURE_PAIRS.map((pair) => [pair.gesture, pair.action, pair.minScore, pair.dwellMs]),
    ).toEqual([
      ['Open_Palm', { kind: 'draft', name: 'capture_room' }, 0.6, 600],
      ['Closed_Fist', { kind: 'draft', name: 'hold' }, 0.8, 600],
      ['Pointing_Up', { kind: 'draft', name: 'takeoff' }, 0.7, 600],
      ['Victory', { kind: 'draft', name: 'translate' }, 0.7, 600],
      ['ILoveYou', { kind: 'draft', name: 'land' }, 0.7, 600],
      ['Thumb_Up', { kind: 'confirm' }, 0.6, 400],
      ['Thumb_Down', { kind: 'cancel' }, 0.7, 400],
    ])
    expect(validateGesturePairs(DEFAULT_GESTURE_PAIRS)).toEqual([])
    expect(DEFAULT_GESTURE_POLICY_CONFIG).toEqual({
      pairs: DEFAULT_GESTURE_PAIRS,
      releaseMs: 200,
      minConsensus: 0.8,
      maxFrameGapMs: 250,
    })
    expect(CONSENSUS_MIN_FRAMES).toBe(4)
    expect(GESTURE_TRANSLATE_STEP).toEqual({ dx: 1, dy: 0 })
    expect([...GESTURE_EMITTABLE_NAMES]).toEqual(['capture_room', 'hold', 'takeoff', 'translate', 'land'])
  })

  test('estop, arm, disarm, land_all, come_home, altitude, formation, spacing, sweep and select are never gesture-emittable, at any threshold', () => {
    // The same check runs against DEFAULT_GESTURE_PAIRS when the module loads;
    // importing it above is proof the tuned defaults passed it.
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
      for (const minScore of [0.6, 0.7, 0.8]) {
        const pair = {
          gesture: 'Victory',
          action: { kind: 'draft', name },
          minScore,
          dwellMs: 600,
        } as unknown as GesturePair
        expect(validateGesturePairs([pair])).toEqual([`${name} is never gesture-emittable.`])
      }
    }
    for (const name of ['capture_room', 'hold', 'takeoff', 'translate', 'land']) {
      expect(isGestureEmittable(name)).toBe(true)
    }
    const offList = {
      gesture: 'Victory',
      action: { kind: 'draft', name: 'survey_area' },
      minScore: 0.7,
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

  test('observeHand feeds the policy the first hand, or no hand, the way the producer does', () => {
    expect(observeHand(10, undefined)).toEqual({ t: 10, category: null, score: 0 })
    expect(observeHand(10, { category: 'Victory', score: 0.7 })).toEqual({ t: 10, category: 'Victory', score: 0.7 })
    expect(observeHand(10, { category: null, score: 0.3 })).toEqual({ t: 10, category: null, score: 0.3 })
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
      frames: 1,
      strongFrames: 1,
    })
    expect(outcomes[6]).toMatchObject({ kind: 'candidate', heldMs: 300, progress: 0.5, frames: 7, strongFrames: 7 })
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
    let count = 0
    script.forEach((observation) => {
      const step = stepGesturePolicy(state, observation)
      if (step.state.phase !== phases[phases.length - 1]) phases.push(step.state.phase)
      if (step.outcome.kind === 'accepted') count += 1
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
    expect(count).toBe(2)
  })

  test('confirm and cancel use the shorter decision dwell', () => {
    const thumbUp = run(held('Thumb_Up', 0, 400))
    expect(thumbUp.outcomes.at(-2)).toMatchObject({ kind: 'candidate', heldMs: 350 })
    expect(thumbUp.outcomes.at(-1)).toMatchObject({ kind: 'accepted', heldMs: 400 })

    const thumbDown = run(held('Thumb_Down', 0, 350))
    expect(kinds(thumbDown.outcomes)).not.toContain('accepted')
    expect(thumbDown.state.phase).toBe('candidate')
  })

  test('a first frame below the pose threshold never starts a candidate; each pose has its own threshold', () => {
    const idle = run([frame(0, 'Open_Palm', 0.59)])
    expect(idle.outcomes).toEqual([
      { kind: 'low_confidence', pair: pairFor('Open_Palm'), score: 0.59, heldMs: 0, frames: 1, strongFrames: 0 },
    ])
    expect(idle.state.phase).toBe('idle')

    expect(run([frame(0, 'Open_Palm', 0.6)]).outcomes[0]).toMatchObject({ kind: 'candidate' })
    expect(run([frame(0, 'Thumb_Up', 0.6)]).outcomes[0]).toMatchObject({ kind: 'candidate' })
    expect(run([frame(0, 'Thumb_Down', 0.69)]).outcomes[0]).toMatchObject({ kind: 'low_confidence' })
    expect(run([frame(0, 'Thumb_Down', 0.7)]).outcomes[0]).toMatchObject({ kind: 'candidate' })
    expect(run([frame(0, 'Closed_Fist', 0.79)]).outcomes[0]).toMatchObject({ kind: 'low_confidence' })
    expect(run([frame(0, 'Closed_Fist', 0.8)]).outcomes[0]).toMatchObject({ kind: 'candidate' })
  })

  describe('consensus over the candidate window', () => {
    const pair = pairFor('Open_Palm')

    test('one weak frame inside the dwell window does not reset the candidate', () => {
      const { outcomes } = run([
        ...held('Open_Palm', 0, 300, 0.7),
        frame(350, 'Open_Palm', 0.4),
        ...held('Open_Palm', 400, 600, 0.7),
      ])
      expect(outcomes[7]).toEqual({
        kind: 'candidate',
        pair,
        heldMs: 350,
        progress: 350 / 600,
        frames: 8,
        strongFrames: 7,
      })
      expect(outcomes[12]).toEqual({ kind: 'accepted', pair, heldMs: 600 })
      expect(kinds(outcomes)).not.toContain('low_confidence')
    })

    test('three weak frames in ten reset the candidate with a low-confidence outcome and it restarts on the next strong frame', () => {
      const scores = [0.7, 0.7, 0.7, 0.7, 0.7, 0.4, 0.7, 0.4, 0.7, 0.4]
      const { outcomes, state } = run(scores.map((score, index) => frame(index * FRAME_MS, 'Open_Palm', score)))
      expect(outcomes[5]).toMatchObject({ kind: 'candidate', frames: 6, strongFrames: 5 })
      expect(outcomes[7]).toEqual({ kind: 'low_confidence', pair, score: 0.4, heldMs: 350, frames: 8, strongFrames: 6 })
      expect(outcomes[8]).toEqual({ kind: 'candidate', pair, heldMs: 0, progress: 0, frames: 1, strongFrames: 1 })
      expect(outcomes[9]).toMatchObject({ kind: 'candidate', frames: 2, strongFrames: 1 })
      expect(kinds(outcomes)).not.toContain('accepted')
      expect(state.phase).toBe('candidate')
    })

    test('the share is judged from the fourth frame, so with four frames every one must be strong', () => {
      const { outcomes, state } = run([
        frame(0, 'Open_Palm', 0.7),
        frame(50, 'Open_Palm', 0.4),
        frame(100, 'Open_Palm', 0.7),
        frame(150, 'Open_Palm', 0.7),
      ])
      expect(kinds(outcomes)).toEqual(['candidate', 'candidate', 'candidate', 'low_confidence'])
      expect(outcomes[3]).toMatchObject({ heldMs: 150, frames: 4, strongFrames: 3 })
      expect(state.phase).toBe('idle')
    })

    test('a category change resets as a dwell timeout whatever the consensus', () => {
      const { outcomes } = run([...held('Open_Palm', 0, 300, 0.7), frame(350, 'Closed_Fist')])
      expect(outcomes[6]).toMatchObject({ kind: 'candidate', frames: 7, strongFrames: 7 })
      expect(outcomes[7]).toEqual({ kind: 'dwell_timeout', pair, heldMs: 350, reason: 'gesture_changed' })
    })

    test('the share is a config value', () => {
      const strict: GesturePolicyConfig = { ...DEFAULT_GESTURE_POLICY_CONFIG, minConsensus: 1 }
      const { outcomes } = run(
        [...held('Open_Palm', 0, 300, 0.7), frame(350, 'Open_Palm', 0.4), ...held('Open_Palm', 400, 600, 0.7)],
        strict,
      )
      expect(outcomes[7]).toMatchObject({ kind: 'low_confidence', frames: 8, strongFrames: 7 })
      expect(kinds(outcomes)).not.toContain('accepted')
    })
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
      frames: 1,
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

  describe('release on pose change', () => {
    const fist = pairFor('Closed_Fist')
    const down = pairFor('Thumb_Down')

    test('after an accepted fist, thumbs down at 0.70 within the release window starts a candidate and is accepted after its dwell; the fist again stays suppressed until neutral', () => {
      const { outcomes, state } = run([
        ...held('Closed_Fist', 0, 600),
        frame(650, 'Closed_Fist'),
        ...held('Thumb_Down', 700, 1100, 0.7),
        ...held('Closed_Fist', 1150, 1300),
        ...held(null, 1350, 1550),
        ...held('Closed_Fist', 1600, 2200),
      ])
      expect(accepted(outcomes)).toEqual([
        { kind: 'accepted', pair: fist, heldMs: 600 },
        { kind: 'accepted', pair: down, heldMs: 400 },
        { kind: 'accepted', pair: fist, heldMs: 600 },
      ])
      expect(outcomes[13]).toEqual({ kind: 'duplicate_suppressed', pair: fist, category: 'Closed_Fist' })
      expect(outcomes[14]).toEqual({ kind: 'candidate', pair: down, heldMs: 0, progress: 0, frames: 1, strongFrames: 1 })
      expect(outcomes[22]).toEqual({ kind: 'accepted', pair: down, heldMs: 400 })
      expect(outcomes.slice(23, 27)).toEqual(
        Array(4).fill({ kind: 'duplicate_suppressed', pair: fist, category: 'Closed_Fist' }),
      )
      expect(kinds(outcomes.slice(27, 31))).toEqual(Array(4).fill('wait_for_release'))
      expect(outcomes[31]).toEqual({ kind: 'released', pair: down })
      expect(outcomes[32]).toMatchObject({ kind: 'candidate', pair: fist, heldMs: 0 })
      expect(outcomes[44]).toEqual({ kind: 'accepted', pair: fist, heldMs: 600 })
      expect(state.phase).toBe('accepted')
    })

    test('a different pose below its own threshold during the release wait stays a suppressed duplicate', () => {
      const { outcomes } = run([...held('Closed_Fist', 0, 600), frame(650, 'Thumb_Down', 0.69), frame(700, 'Thumb_Down', 0.7)])
      expect(outcomes[13]).toEqual({ kind: 'duplicate_suppressed', pair: fist, category: 'Thumb_Down' })
      expect(outcomes[14]).toMatchObject({ kind: 'candidate', pair: down, heldMs: 0 })
    })

    test('a candidate started during the release wait that fails keeps the accepted pose suppressed until neutral', () => {
      const { outcomes } = run([
        ...held('Closed_Fist', 0, 600),
        ...held('Thumb_Down', 650, 750, 0.7),
        frame(800, null),
        frame(850, 'Closed_Fist'),
        ...held(null, 900, 1100),
        ...held('Closed_Fist', 1150, 1750),
      ])
      expect(kinds(outcomes.slice(13, 16))).toEqual(Array(3).fill('candidate'))
      expect(outcomes[16]).toEqual({ kind: 'dwell_timeout', pair: down, heldMs: 150, reason: 'released' })
      expect(outcomes[17]).toEqual({ kind: 'duplicate_suppressed', pair: fist, category: 'Closed_Fist' })
      expect(kinds(outcomes.slice(18, 22))).toEqual(Array(4).fill('wait_for_release'))
      expect(outcomes[22]).toEqual({ kind: 'released', pair: fist })
      expect(accepted(outcomes)).toEqual([
        { kind: 'accepted', pair: fist, heldMs: 600 },
        { kind: 'accepted', pair: fist, heldMs: 600 },
      ])
    })

    test('a confirm can follow a draft without the hand leaving the frame, and a repeat of either is suppressed', () => {
      const palm = pairFor('Open_Palm')
      const up = pairFor('Thumb_Up')
      const { outcomes } = run([
        ...held('Open_Palm', 0, 600),
        ...held('Thumb_Up', 650, 1050),
        ...held('Thumb_Up', 1100, 1200),
        ...held('Open_Palm', 1250, 1350),
        ...held(null, 1400, 1600),
        ...held('Thumb_Up', 1650, 2050),
      ])
      expect(accepted(outcomes)).toEqual([
        { kind: 'accepted', pair: palm, heldMs: 600 },
        { kind: 'accepted', pair: up, heldMs: 400 },
        { kind: 'accepted', pair: up, heldMs: 400 },
      ])
      expect(outcomes[22]).toEqual({ kind: 'duplicate_suppressed', pair: up, category: 'Thumb_Up' })
      expect(outcomes[25]).toEqual({ kind: 'duplicate_suppressed', pair: palm, category: 'Open_Palm' })
      expect(outcomes[32]).toEqual({ kind: 'released', pair: up })
    })
  })

  describe.each([
    ['Pointing_Up', 'takeoff'],
    ['Victory', 'translate'],
    ['ILoveYou', 'land'],
  ] as const)('%s drafts %s', (gesture, name) => {
    test('is accepted once after the 600 ms draft dwell at score 0.70, then suppressed until neutral', () => {
      const pair = pairFor(gesture)
      expect(pair).toMatchObject({ action: { kind: 'draft', name }, minScore: 0.7, dwellMs: 600 })
      const { outcomes, state } = run([...held(gesture, 0, 600, 0.7), ...held(gesture, 650, 900), ...held(null, 950, 1150)])
      expect(outcomes[0]).toEqual({ kind: 'candidate', pair, heldMs: 0, progress: 0, frames: 1, strongFrames: 1 })
      expect(outcomes[11]).toMatchObject({ kind: 'candidate', heldMs: 550, frames: 12, strongFrames: 12 })
      expect(outcomes[12]).toEqual({ kind: 'accepted', pair, heldMs: 600 })
      expect(kinds(outcomes).filter((kind) => kind === 'accepted')).toHaveLength(1)
      expect(kinds(outcomes.slice(13, 19))).toEqual(Array(6).fill('duplicate_suppressed'))
      expect(outcomes[13]).toMatchObject({ kind: 'duplicate_suppressed', pair, category: gesture })
      expect(outcomes.at(-1)).toEqual({ kind: 'released', pair })
      expect(state.phase).toBe('idle')
    })

    test('a frame below 0.70 never starts a candidate, and a window below consensus abandons one', () => {
      const pair = pairFor(gesture)
      const idle = run([frame(0, gesture, 0.69)])
      expect(idle.outcomes).toEqual([{ kind: 'low_confidence', pair, score: 0.69, heldMs: 0, frames: 1, strongFrames: 0 }])
      expect(idle.state.phase).toBe('idle')

      const dip = run([...held(gesture, 0, 100), frame(150, gesture, 0.5), ...held(gesture, 200, 750)])
      expect(dip.outcomes[3]).toEqual({ kind: 'low_confidence', pair, score: 0.5, heldMs: 150, frames: 4, strongFrames: 3 })
      expect(dip.outcomes[4]).toMatchObject({ kind: 'candidate', heldMs: 0, frames: 1 })
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
      expect(accepted(outcomes)).toEqual([{ kind: 'accepted', pair, heldMs: 600 }])
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
    expect(accepted(outcomes)).toHaveLength(1)
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
      minConsensus: 0.8,
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
