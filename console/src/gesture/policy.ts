/**
 * Pure gesture-to-intent policy. No DOM, no MediaPipe, no clock of its own:
 * every step receives one observation stamped with monotonic milliseconds and
 * returns the next state plus one outcome. The hook turns `accepted` outcomes
 * into Intent v1 drafts and decisions; nothing here emits anything.
 *
 * Gesture-to-intent mapping (MediaPipe built-in gesture classes):
 *
 * | Gesture     | Held for | Score | Action                                   |
 * |-------------|----------|-------|------------------------------------------|
 * | Open_Palm   | 600 ms   | ≥ 0.8 | draft `capture_room` (preview, not sent)  |
 * | Closed_Fist | 600 ms   | ≥ 0.8 | draft `hold` (preview, not sent)          |
 * | Thumb_Up    | 400 ms   | ≥ 0.8 | confirm the pending preview               |
 * | Thumb_Down  | 400 ms   | ≥ 0.8 | cancel the pending preview                |
 *
 * `estop`, `arm`, `takeoff`, and free-flight motion are never gesture-emittable;
 * they stay on the console controls and the physical RC. See
 * NEVER_GESTURE_EMITTABLE and validateGesturePairs.
 */
import type { ConsoleIntentName } from '../relay/contract'

export type GestureCategory =
  | 'None'
  | 'Closed_Fist'
  | 'Open_Palm'
  | 'Pointing_Up'
  | 'Thumb_Down'
  | 'Thumb_Up'
  | 'Victory'
  | 'ILoveYou'

export const GESTURE_CATEGORIES: readonly GestureCategory[] = [
  'None',
  'Closed_Fist',
  'Open_Palm',
  'Pointing_Up',
  'Thumb_Down',
  'Thumb_Up',
  'Victory',
  'ILoveYou',
]

export type GestureEmittableName = Extract<ConsoleIntentName, 'capture_room' | 'hold'>

export type GestureAction =
  | { kind: 'draft'; name: GestureEmittableName }
  | { kind: 'confirm' }
  | { kind: 'cancel' }

export interface GesturePair {
  gesture: GestureCategory
  action: GestureAction
  /** Minimum recognizer score for a frame to count toward the dwell. */
  minScore: number
  /** Continuous hold required before the pair is accepted. */
  dwellMs: number
}

/** The only Intent v1 names a gesture may draft. */
export const GESTURE_EMITTABLE_NAMES: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'capture_room',
  'hold',
])

/**
 * Names that no gesture pair may ever target. The network stop, arming,
 * takeoff, and every free-flight motion stay on the console controls and the
 * physical RC. This list is checked against every pair set, including the
 * default, and is shown in the panel.
 */
export const NEVER_GESTURE_EMITTABLE: readonly string[] = Object.freeze([
  'estop',
  'arm',
  'disarm',
  'takeoff',
  'land',
  'land_all',
  'translate',
  'altitude',
  'formation_next',
  'formation_set',
  'spacing',
  'come_home',
  'sweep',
  'select',
])

export const DEFAULT_MIN_SCORE = 0.8
export const DRAFT_DWELL_MS = 600
export const DECISION_DWELL_MS = 400
/** Neutral must hold this long after an acceptance before a new candidate can start. */
export const DEFAULT_RELEASE_MS = 200
/** A candidate whose frames stop arriving for longer than this cannot prove its dwell. */
export const DEFAULT_MAX_FRAME_GAP_MS = 250

export const DEFAULT_GESTURE_PAIRS: readonly GesturePair[] = Object.freeze([
  {
    gesture: 'Open_Palm',
    action: { kind: 'draft', name: 'capture_room' },
    minScore: DEFAULT_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Closed_Fist',
    action: { kind: 'draft', name: 'hold' },
    minScore: DEFAULT_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Thumb_Up',
    action: { kind: 'confirm' },
    minScore: DEFAULT_MIN_SCORE,
    dwellMs: DECISION_DWELL_MS,
  },
  {
    gesture: 'Thumb_Down',
    action: { kind: 'cancel' },
    minScore: DEFAULT_MIN_SCORE,
    dwellMs: DECISION_DWELL_MS,
  },
])

export interface GesturePolicyConfig {
  pairs: readonly GesturePair[]
  releaseMs: number
  maxFrameGapMs: number
}

export const DEFAULT_GESTURE_POLICY_CONFIG: GesturePolicyConfig = Object.freeze({
  pairs: DEFAULT_GESTURE_PAIRS,
  releaseMs: DEFAULT_RELEASE_MS,
  maxFrameGapMs: DEFAULT_MAX_FRAME_GAP_MS,
})

/** Returns every reason a pair set is unacceptable; empty means acceptable. */
export function validateGesturePairs(pairs: readonly GesturePair[]): string[] {
  const problems: string[] = []
  const seen = new Set<GestureCategory>()
  pairs.forEach((pair) => {
    if (pair.gesture === 'None') problems.push('None cannot be mapped; it is the neutral release state.')
    if (seen.has(pair.gesture)) problems.push(`${pair.gesture} is mapped more than once.`)
    seen.add(pair.gesture)
    if (pair.action.kind === 'draft') {
      const name: string = pair.action.name
      if (NEVER_GESTURE_EMITTABLE.includes(name)) {
        problems.push(`${name} is never gesture-emittable.`)
      } else if (!GESTURE_EMITTABLE_NAMES.has(pair.action.name)) {
        problems.push(`${name} is not on the gesture-emittable allowlist.`)
      }
    }
    if (!(pair.minScore > 0 && pair.minScore <= 1)) {
      problems.push(`${pair.gesture} needs a score threshold in (0, 1].`)
    }
    if (!(pair.dwellMs > 0)) problems.push(`${pair.gesture} needs a positive dwell.`)
  })
  return problems
}

export function isGestureEmittable(name: string): name is GestureEmittableName {
  return (
    GESTURE_EMITTABLE_NAMES.has(name as ConsoleIntentName) && !NEVER_GESTURE_EMITTABLE.includes(name)
  )
}

{
  const problems = validateGesturePairs(DEFAULT_GESTURE_PAIRS)
  if (problems.length > 0) {
    throw new Error(`Default gesture pairs are invalid: ${problems.join(' ')}`)
  }
}

export interface GestureObservation {
  /** Monotonic milliseconds (performance.now), never wall-clock. */
  t: number
  /** Best gesture class for the tracked hand, or null when no hand is present. */
  category: GestureCategory | null
  score: number
}

export type GesturePhase = 'idle' | 'candidate' | 'accepted' | 'wait_for_release'

export interface GesturePolicyState {
  phase: GesturePhase
  pair: GesturePair | null
  /** Candidate start while `candidate`; neutral start while `wait_for_release`. */
  since: number | null
  lastFrameAt: number | null
}

export type DwellTimeoutReason = 'released' | 'gesture_changed' | 'frame_gap'

export type GesturePolicyOutcome =
  | { kind: 'idle' }
  | { kind: 'unmapped'; category: GestureCategory }
  | { kind: 'low_confidence'; pair: GesturePair; score: number }
  | { kind: 'candidate'; pair: GesturePair; heldMs: number; progress: number }
  | { kind: 'accepted'; pair: GesturePair; heldMs: number }
  | { kind: 'dwell_timeout'; pair: GesturePair; heldMs: number; reason: DwellTimeoutReason }
  | { kind: 'duplicate_suppressed'; pair: GesturePair; category: GestureCategory }
  | { kind: 'wait_for_release'; pair: GesturePair; neutralMs: number }
  | { kind: 'released'; pair: GesturePair }

export interface GesturePolicyStep {
  state: GesturePolicyState
  outcome: GesturePolicyOutcome
}

export function createGesturePolicyState(): GesturePolicyState {
  return { phase: 'idle', pair: null, since: null, lastFrameAt: null }
}

export function stepGesturePolicy(
  state: GesturePolicyState,
  observation: GestureObservation,
  config: GesturePolicyConfig = DEFAULT_GESTURE_POLICY_CONFIG,
): GesturePolicyStep {
  const { t } = observation
  const category = observation.category === 'None' ? null : observation.category
  const pair = category === null ? null : (config.pairs.find((item) => item.gesture === category) ?? null)

  switch (state.phase) {
    case 'idle': {
      if (category === null) return { state: idle(t), outcome: { kind: 'idle' } }
      if (!pair) return { state: idle(t), outcome: { kind: 'unmapped', category } }
      if (observation.score < pair.minScore) {
        return { state: idle(t), outcome: { kind: 'low_confidence', pair, score: observation.score } }
      }
      return {
        state: { phase: 'candidate', pair, since: t, lastFrameAt: t },
        outcome: { kind: 'candidate', pair, heldMs: 0, progress: 0 },
      }
    }
    case 'candidate': {
      const current = state.pair as GesturePair
      const since = state.since ?? t
      if (state.lastFrameAt !== null && t - state.lastFrameAt > config.maxFrameGapMs) {
        return {
          state: idle(t),
          outcome: {
            kind: 'dwell_timeout',
            pair: current,
            heldMs: Math.max(0, state.lastFrameAt - since),
            reason: 'frame_gap',
          },
        }
      }
      const heldMs = Math.max(0, t - since)
      if (category === null) {
        return {
          state: idle(t),
          outcome: { kind: 'dwell_timeout', pair: current, heldMs, reason: 'released' },
        }
      }
      if (pair !== current) {
        return {
          state: idle(t),
          outcome: { kind: 'dwell_timeout', pair: current, heldMs, reason: 'gesture_changed' },
        }
      }
      if (observation.score < pair.minScore) {
        return { state: idle(t), outcome: { kind: 'low_confidence', pair, score: observation.score } }
      }
      if (heldMs >= pair.dwellMs) {
        return {
          state: { phase: 'accepted', pair, since: null, lastFrameAt: t },
          outcome: { kind: 'accepted', pair, heldMs },
        }
      }
      return {
        state: { ...state, lastFrameAt: t },
        outcome: { kind: 'candidate', pair, heldMs, progress: Math.min(1, heldMs / pair.dwellMs) },
      }
    }
    case 'accepted':
    case 'wait_for_release': {
      const current = state.pair as GesturePair
      if (category !== null) {
        return {
          state: { phase: 'wait_for_release', pair: current, since: null, lastFrameAt: t },
          outcome: { kind: 'duplicate_suppressed', pair: current, category },
        }
      }
      const since = state.phase === 'wait_for_release' && state.since !== null ? state.since : t
      const neutralMs = Math.max(0, t - since)
      if (neutralMs >= config.releaseMs) {
        return { state: idle(t), outcome: { kind: 'released', pair: current } }
      }
      return {
        state: { phase: 'wait_for_release', pair: current, since, lastFrameAt: t },
        outcome: { kind: 'wait_for_release', pair: current, neutralMs },
      }
    }
  }
}

function idle(t: number): GesturePolicyState {
  return { phase: 'idle', pair: null, since: null, lastFrameAt: t }
}

export function describeGestureAction(action: GestureAction): string {
  if (action.kind === 'draft') return `draft ${action.name}`
  return action.kind === 'confirm' ? 'confirm pending preview' : 'cancel pending preview'
}
