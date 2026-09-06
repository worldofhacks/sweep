/**
 * Pure gesture-to-intent policy. No DOM, no MediaPipe, no clock of its own:
 * every step receives one observation stamped with monotonic milliseconds and
 * returns the next state plus one outcome. The hook turns `accepted` outcomes
 * into Intent v1 drafts and decisions; nothing here emits anything.
 *
 * Gesture-to-intent mapping (MediaPipe built-in gesture classes):
 *
 * | Gesture     | Held for | Score  | Action                                          |
 * |-------------|----------|--------|-------------------------------------------------|
 * | Open_Palm   | 600 ms   | ≥ 0.60 | draft `capture_room` (preview, not sent)         |
 * | Closed_Fist | 600 ms   | ≥ 0.80 | draft `hold` (preview, not sent)                 |
 * | Pointing_Up | 600 ms   | ≥ 0.70 | draft `takeoff` (preview, not sent)              |
 * | Victory     | 600 ms   | ≥ 0.70 | draft `translate` forward one step (preview)     |
 * | ILoveYou    | 600 ms   | ≥ 0.70 | draft `land` (preview, not sent)                 |
 * | Thumb_Up    | 400 ms   | ≥ 0.60 | confirm the pending preview                      |
 * | Thumb_Down  | 400 ms   | ≥ 0.70 | cancel the pending preview                       |
 *
 * A candidate starts on a frame at or above the pose's score and keeps building
 * while the top category stays the same and at least DEFAULT_MIN_CONSENSUS of
 * its frames since it started scored at or above that threshold; acceptance
 * still needs the full dwell of continuous candidate time. Every pose accepted
 * since the hand was last neutral for releaseMs stays suppressed until it is
 * neutral for that long again, while a different mapped pose at or above its
 * own score starts a new candidate at once.
 *
 * Every draft lands in the confirmation dock; only the confirm gesture or the
 * dock button sends it. `estop`, `arm`, `disarm`, `land_all`, `come_home`,
 * `altitude`, `formation_next`, `formation_set`, `spacing`, `sweep`, and
 * `select` are never gesture-emittable; they stay on the console controls and
 * the physical RC. See NEVER_GESTURE_EMITTABLE and validateGesturePairs.
 */
import type { ConsoleIntentName, TranslateArgs } from '../relay/contract'

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

export type GestureEmittableName = Extract<
  ConsoleIntentName,
  'capture_room' | 'hold' | 'takeoff' | 'translate' | 'land'
>

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

/**
 * The only Intent v1 names a gesture may draft. Each one is parked in the
 * confirmation dock; the relay's webcam allowlist mirrors this set.
 */
export const GESTURE_EMITTABLE_NAMES: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'capture_room',
  'hold',
  'takeoff',
  'translate',
  'land',
])

/**
 * The one translate a gesture may draft: a single step along +x of the
 * planner's translation frame. With `translation_frame` aircraft_relative the
 * planner rotates it by the aircraft heading, so it is one step forward.
 */
export const GESTURE_TRANSLATE_STEP: Readonly<TranslateArgs> = Object.freeze({ dx: 1, dy: 0 })

/**
 * Names that no gesture pair may ever target. The network stop, arming and
 * disarming, the fleet-wide land, come home, altitude, formation, spacing,
 * sweep, and selection stay on the console controls and the physical RC. This
 * list is checked against every pair set, including the default, and is shown
 * in the panel.
 */
export const NEVER_GESTURE_EMITTABLE: readonly string[] = Object.freeze([
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

/**
 * Per-pose score thresholds. The four tuned numbers come from the recorded
 * webcam session `src/testing/gesture-recordings/webcam-session-1.jsonl`
 * (1499 recognizer frames over 50 s; the operator held Closed_Fist six times
 * and Thumb_Down, Thumb_Up, and Open_Palm three times each, every label
 * correct):
 *
 * | Pose        | n   | median | ≥ 0.80 | ≥ 0.60 | threshold |
 * |-------------|-----|--------|--------|--------|-----------|
 * | Closed_Fist | 330 | 0.85   | 96%    | 99%    | 0.80      |
 * | Thumb_Down  | 163 | 0.82   | 59%    | 98%    | 0.70      |
 * | Thumb_Up    | 194 | 0.70   | 0%     | 95%    | 0.60      |
 * | Open_Palm   | 181 | 0.69   | 0%     | 99%    | 0.60      |
 *
 * Pointing_Up, Victory, and ILoveYou have no recording yet and sit at
 * UNTUNED_MIN_SCORE until one exists. To re-tune: enable tracking in the
 * Gesture module, hold each pose three times, Download session (JSONL), strip
 * the landmarks (`jq -c 'if .kind == "recognizer" then .hands |= map(del(.landmarks)) else . end'`,
 * or `stripGestureRecording` in `src/testing/gesture-recordings.ts`), drop the
 * file into `src/testing/gesture-recordings/`, and run
 * `pnpm vitest run src/gesture/policy.eval.test.ts`: it prints each pose's score
 * distribution and how many times the replay accepted it, so a threshold can be
 * read off the distribution and checked against the replay in one step.
 */
export const OPEN_PALM_MIN_SCORE = 0.6
export const CLOSED_FIST_MIN_SCORE = 0.8
export const THUMB_UP_MIN_SCORE = 0.6
export const THUMB_DOWN_MIN_SCORE = 0.7
/** Poses without a recording of their own yet. */
export const UNTUNED_MIN_SCORE = 0.7
export const DRAFT_DWELL_MS = 600
export const DECISION_DWELL_MS = 400
/** Neutral must hold this long after an acceptance before the same pose can be accepted again. */
export const DEFAULT_RELEASE_MS = 200
/**
 * Share of a candidate's frames (since it started) that must score at or above
 * the pair's minScore for the candidate to keep building; checked on every frame
 * once CONSENSUS_MIN_FRAMES have arrived.
 */
export const DEFAULT_MIN_CONSENSUS = 0.8
/** The consensus rule needs this many candidate frames before it can reset a candidate. */
export const CONSENSUS_MIN_FRAMES = 4
/** A candidate whose frames stop arriving for longer than this cannot prove its dwell. */
export const DEFAULT_MAX_FRAME_GAP_MS = 250

export const DEFAULT_GESTURE_PAIRS: readonly GesturePair[] = Object.freeze([
  {
    gesture: 'Open_Palm',
    action: { kind: 'draft', name: 'capture_room' },
    minScore: OPEN_PALM_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Closed_Fist',
    action: { kind: 'draft', name: 'hold' },
    minScore: CLOSED_FIST_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Pointing_Up',
    action: { kind: 'draft', name: 'takeoff' },
    minScore: UNTUNED_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Victory',
    action: { kind: 'draft', name: 'translate' },
    minScore: UNTUNED_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'ILoveYou',
    action: { kind: 'draft', name: 'land' },
    minScore: UNTUNED_MIN_SCORE,
    dwellMs: DRAFT_DWELL_MS,
  },
  {
    gesture: 'Thumb_Up',
    action: { kind: 'confirm' },
    minScore: THUMB_UP_MIN_SCORE,
    dwellMs: DECISION_DWELL_MS,
  },
  {
    gesture: 'Thumb_Down',
    action: { kind: 'cancel' },
    minScore: THUMB_DOWN_MIN_SCORE,
    dwellMs: DECISION_DWELL_MS,
  },
])

export interface GesturePolicyConfig {
  pairs: readonly GesturePair[]
  releaseMs: number
  /** Share of candidate frames at or above the pair's minScore needed to keep building; see DEFAULT_MIN_CONSENSUS. */
  minConsensus: number
  maxFrameGapMs: number
}

export const DEFAULT_GESTURE_POLICY_CONFIG: GesturePolicyConfig = Object.freeze({
  pairs: DEFAULT_GESTURE_PAIRS,
  releaseMs: DEFAULT_RELEASE_MS,
  minConsensus: DEFAULT_MIN_CONSENSUS,
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
  NEVER_GESTURE_EMITTABLE.forEach((name) => {
    if (GESTURE_EMITTABLE_NAMES.has(name as ConsoleIntentName)) {
      problems.push(`${name} is both never-emittable and on the gesture-emittable allowlist.`)
    }
  })
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

/**
 * The observation the producer feeds the policy for one recognizer frame: the
 * first tracked hand, or no hand. The recorded-session eval replays frames
 * through this same function so it sees exactly what the live loop saw.
 */
export function observeHand(
  t: number,
  hand: { category: GestureCategory | null; score: number } | undefined,
): GestureObservation {
  return { t, category: hand?.category ?? null, score: hand?.score ?? 0 }
}

export type GesturePhase = 'idle' | 'candidate' | 'accepted' | 'wait_for_release'

export interface GesturePolicyState {
  phase: GesturePhase
  /** The candidate pair while `candidate`; the most recently accepted pair while `accepted` or `wait_for_release`. */
  pair: GesturePair | null
  /** Candidate start while `candidate`; neutral start while `wait_for_release`. */
  since: number | null
  lastFrameAt: number | null
  /** Frames counted toward the current candidate, including the one that started it. */
  frames: number
  /** Of those, the frames that scored at or above the pair's minScore. */
  strongFrames: number
  /**
   * Every pair accepted since the hand was last neutral for releaseMs. Each
   * stays suppressed until then; a mapped pose outside this list may start a
   * new candidate at once.
   */
  suppressed: readonly GesturePair[]
}

export type DwellTimeoutReason = 'released' | 'gesture_changed' | 'frame_gap'

export type GesturePolicyOutcome =
  | { kind: 'idle' }
  | { kind: 'unmapped'; category: GestureCategory }
  /**
   * From idle: one frame below the pair's minScore (frames 1, strongFrames 0)
   * that started nothing. From a candidate: the consensus over its frames fell
   * below minConsensus, so it was abandoned after heldMs.
   */
  | {
      kind: 'low_confidence'
      pair: GesturePair
      score: number
      heldMs: number
      frames: number
      strongFrames: number
    }
  | {
      kind: 'candidate'
      pair: GesturePair
      heldMs: number
      progress: number
      frames: number
      strongFrames: number
    }
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
  return idle(null)
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
        return {
          state: idle(t),
          outcome: { kind: 'low_confidence', pair, score: observation.score, heldMs: 0, frames: 1, strongFrames: 0 },
        }
      }
      return startCandidate(pair, t, [])
    }
    case 'candidate': {
      const current = state.pair as GesturePair
      const since = state.since ?? t
      if (state.lastFrameAt !== null && t - state.lastFrameAt > config.maxFrameGapMs) {
        return {
          state: abandonCandidate(state, t, category === null),
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
          state: abandonCandidate(state, t, true),
          outcome: { kind: 'dwell_timeout', pair: current, heldMs, reason: 'released' },
        }
      }
      if (pair !== current) {
        return {
          state: abandonCandidate(state, t, false),
          outcome: { kind: 'dwell_timeout', pair: current, heldMs, reason: 'gesture_changed' },
        }
      }
      const frames = state.frames + 1
      const strongFrames = state.strongFrames + (observation.score >= pair.minScore ? 1 : 0)
      if (frames >= CONSENSUS_MIN_FRAMES && strongFrames / frames < config.minConsensus) {
        return {
          state: abandonCandidate(state, t, false),
          outcome: { kind: 'low_confidence', pair, score: observation.score, heldMs, frames, strongFrames },
        }
      }
      if (heldMs >= pair.dwellMs) {
        return {
          state: {
            phase: 'accepted',
            pair,
            since: null,
            lastFrameAt: t,
            frames,
            strongFrames,
            suppressed: [...state.suppressed, pair],
          },
          outcome: { kind: 'accepted', pair, heldMs },
        }
      }
      return {
        state: { ...state, lastFrameAt: t, frames, strongFrames },
        outcome: {
          kind: 'candidate',
          pair,
          heldMs,
          progress: Math.min(1, heldMs / pair.dwellMs),
          frames,
          strongFrames,
        },
      }
    }
    case 'accepted':
    case 'wait_for_release': {
      const current = state.pair as GesturePair
      if (category !== null) {
        const repeated = state.suppressed.find((item) => item.gesture === category)
        // A mapped pose not accepted since the last neutral, at or above its own
        // score, ends the release wait and starts its own candidate; every pose
        // accepted since then stays suppressed.
        if (pair && !repeated && observation.score >= pair.minScore) return startCandidate(pair, t, state.suppressed)
        return {
          state: waitForRelease(state, current, null, t),
          outcome: { kind: 'duplicate_suppressed', pair: repeated ?? current, category },
        }
      }
      const since = state.phase === 'wait_for_release' && state.since !== null ? state.since : t
      const neutralMs = Math.max(0, t - since)
      if (neutralMs >= config.releaseMs) {
        return { state: idle(t), outcome: { kind: 'released', pair: current } }
      }
      return {
        state: waitForRelease(state, current, since, t),
        outcome: { kind: 'wait_for_release', pair: current, neutralMs },
      }
    }
  }
}

function idle(t: number | null): GesturePolicyState {
  return { phase: 'idle', pair: null, since: null, lastFrameAt: t, frames: 0, strongFrames: 0, suppressed: [] }
}

/** The frame that starts a candidate is its first counted frame and is strong by construction. */
function startCandidate(pair: GesturePair, t: number, suppressed: readonly GesturePair[]): GesturePolicyStep {
  return {
    state: { phase: 'candidate', pair, since: t, lastFrameAt: t, frames: 1, strongFrames: 1, suppressed },
    outcome: { kind: 'candidate', pair, heldMs: 0, progress: 0, frames: 1, strongFrames: 1 },
  }
}

function waitForRelease(
  state: GesturePolicyState,
  accepted: GesturePair,
  since: number | null,
  t: number,
): GesturePolicyState {
  return {
    phase: 'wait_for_release',
    pair: accepted,
    since,
    lastFrameAt: t,
    frames: 0,
    strongFrames: 0,
    suppressed: state.suppressed,
  }
}

/**
 * A candidate that fails returns to idle, unless poses accepted since the last
 * neutral are still suppressed: then the release wait resumes, counting neutral
 * from this frame when the hand is absent.
 */
function abandonCandidate(state: GesturePolicyState, t: number, neutral: boolean): GesturePolicyState {
  if (state.suppressed.length === 0) return idle(t)
  const accepted = state.suppressed[state.suppressed.length - 1]
  return waitForRelease(state, accepted, neutral ? t : null, t)
}

/** What a draft pair drafts, as the panel and the recorder name it. */
export function describeGestureDraft(name: GestureEmittableName): string {
  return name === 'translate' ? 'translate forward one step' : name
}

export function describeGestureAction(action: GestureAction): string {
  if (action.kind === 'draft') return `draft ${describeGestureDraft(action.name)}`
  return action.kind === 'confirm' ? 'confirm pending preview' : 'cancel pending preview'
}

/** "Open_Palm" reads as "Open palm", the way the design names poses. */
export function gestureLabel(category: GestureCategory): string {
  if (category === 'ILoveYou') return 'I love you'
  const words = category.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1).toLowerCase()
}
