import { describe, expect, test } from 'vitest'
import {
  DEFAULT_GESTURE_POLICY_CONFIG, FLIGHT_GESTURE_POLICY_CONFIG, FLEET_GESTURE_POLICY_CONFIG,
  SWARM_GESTURE_POLICY_CONFIG, createGesturePolicyState, stepGesturePolicy,
  type GestureCategory, type GestureObservation, type GesturePolicyConfig,
} from './policy'

const held = (category: GestureCategory | null, from: number, to: number, score = 0.95): GestureObservation[] =>
  Array.from({ length: (to - from) / 50 + 1 }, (_, i) => ({ t: from + i * 50, category, score }))
function run(frames: GestureObservation[], config: GesturePolicyConfig = DEFAULT_GESTURE_POLICY_CONFIG) {
  let state = createGesturePolicyState()
  return frames.map((frame) => {
    const step = stepGesturePolicy(state, frame, config)
    state = step.state
    return step.outcome
  })
}
const profiles = [DEFAULT_GESTURE_POLICY_CONFIG, FLIGHT_GESTURE_POLICY_CONFIG, FLEET_GESTURE_POLICY_CONFIG, SWARM_GESTURE_POLICY_CONFIG]

describe('tuned dwell preserves confirmation and repeat barriers', () => {
  test.each(profiles)('each profile accepts a calibrated palm with one weak frame but only a strong final frame', (config) => {
    const script = held('Open_Palm', 0, 550, 0.69)
    script[7].score = 0.4
    script.push({ t: 600, category: 'Open_Palm', score: 0.4 }, { t: 650, category: 'Open_Palm', score: 0.69 })
    const outcomes = run(script, config)
    expect(outcomes[12]).toMatchObject({ kind: 'candidate', heldMs: 600, frames: 13, strongFrames: 11 })
    expect(outcomes[13]).toMatchObject({ kind: 'accepted', heldMs: 650 })
  })
  test('a weak-frame majority abandons the candidate without accepting', () => {
    const outcomes = run([{ t: 0, category: 'Thumb_Up', score: 0.7 }, ...held('Thumb_Up', 50, 450, 0.5)])
    expect(outcomes[3]).toMatchObject({ kind: 'low_confidence', frames: 4, strongFrames: 1 })
    expect(outcomes.some((outcome) => outcome.kind === 'accepted')).toBe(false)
  })
  test('sparse decision frames cannot pass the dwell before they establish consensus', () => {
    const outcomes = run([
      { t: 0, category: 'Thumb_Up', score: 0.7 },
      { t: 200, category: 'Thumb_Up', score: 0.5 },
      { t: 400, category: 'Thumb_Up', score: 0.7 },
      { t: 450, category: 'Thumb_Up', score: 0.7 },
    ])
    expect(outcomes[2]).toMatchObject({ kind: 'candidate', heldMs: 400, frames: 3, strongFrames: 2 })
    expect(outcomes[3]).toMatchObject({ kind: 'low_confidence', frames: 4, strongFrames: 3 })
    expect(outcomes.some((outcome) => outcome.kind === 'accepted')).toBe(false)
  })
  test.each(profiles)('changing directly from a draft pose to confirmation never sends a decision', (config) => {
    const outcomes = run([...held('Open_Palm', 0, 600), ...held('Thumb_Up', 650, 1300)], config)
    expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toHaveLength(1)
    expect(outcomes.at(-1)?.kind).toBe('duplicate_suppressed')
  })
  test.each(profiles)('cancel also requires neutral and holding a decision cannot repeat it', (config) => {
    const outcomes = run([
      ...held('Open_Palm', 0, 600), ...held('Thumb_Down', 650, 1100),
      ...held(null, 1150, 1350), ...held('Thumb_Down', 1400, 2300),
    ], config)
    const decisions = outcomes.filter((outcome) => outcome.kind === 'accepted' && outcome.pair.action.kind === 'cancel')
    expect(decisions).toHaveLength(1)
  })
  test('Flight forbids pose-change shortcuts between different motion drafts', () => {
    const outcomes = run([...held('Victory', 0, 600), ...held('Closed_Fist', 650, 1400)], FLIGHT_GESTURE_POLICY_CONFIG)
    expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toHaveLength(1)
  })
  test('Fleet can recognize a different draft pose, but accepted poses cannot cycle until neutral', () => {
    const outcomes = run([
      ...held('Victory', 0, 600), ...held('Closed_Fist', 650, 1250),
      ...held('Victory', 1300, 2000), ...held('Closed_Fist', 2050, 2700),
    ], FLEET_GESTURE_POLICY_CONFIG)
    expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toHaveLength(2)
    expect(outcomes.at(-1)?.kind).toBe('duplicate_suppressed')
  })
  test('a stalled camera cannot establish neutral release between its first and next absent-hand frame', () => {
    const outcomes = run([
      ...held('Victory', 0, 600), { t: 650, category: null, score: 0 },
      { t: 2000, category: null, score: 0 }, ...held('Thumb_Up', 2050, 2550),
    ], FLIGHT_GESTURE_POLICY_CONFIG)
    expect(outcomes.filter((outcome) => outcome.kind === 'accepted')).toHaveLength(1)
    expect(outcomes[14]).toMatchObject({ kind: 'wait_for_release', neutralMs: 0 })
  })
  test('dwell restarts on a backwards clock sample or a camera frame gap', () => {
    for (const t of [-50, 1000]) {
      const outcomes = run([...held('Thumb_Up', 0, 200), { t, category: 'Thumb_Up', score: 0.8 }])
      expect(outcomes.at(-1)).toMatchObject({ kind: 'dwell_timeout', reason: 'frame_gap' })
      expect(outcomes.some((outcome) => outcome.kind === 'accepted')).toBe(false)
    }
  })
})
