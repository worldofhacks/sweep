/**
 * Replays recorded webcam sessions through the gesture policy with the default
 * pairs. `webcam-session-1` is the operator's own session (see the header
 * comment in the fixture): Closed_Fist, Thumb_Down, Thumb_Up, and Open_Palm
 * held three times each with a neutral hand between holds. Each frame goes
 * through `observeHand` and `stepGesturePolicy` exactly as the live loop feeds
 * them, so the replay is the policy's behaviour on that session.
 *
 * The test names carry every pose's score distribution and acceptance count,
 * so `pnpm vitest run src/gesture/policy.eval.test.ts` doubles as the tuning
 * readout named in policy.ts. To tune a pose, record it the same way, strip
 * the landmarks, add the file to RECORDINGS with what the operator performed,
 * and read the numbers off the run. Names are plain template strings rather
 * than test.each placeholders because vitest's printf-style formatter mangles
 * a literal percent sign next to a number.
 */
import { describe, expect, test } from 'vitest'
import { parseGestureRecording, type GestureRecording, type RecordedHand } from '../testing/gesture-recordings'
import webcamSession1 from '../testing/gesture-recordings/webcam-session-1.jsonl?raw'
import {
  DEFAULT_GESTURE_PAIRS,
  DEFAULT_GESTURE_POLICY_CONFIG,
  GESTURE_CATEGORIES,
  createGesturePolicyState,
  observeHand,
  stepGesturePolicy,
  type GestureCategory,
  type GesturePair,
  type GesturePolicyConfig,
} from './policy'

/** The single threshold every pair carried before the per-pose tuning. */
const OLD_FIXED_MIN_SCORE = 0.8

interface Recording {
  file: string
  jsonl: string
  /** Poses the operator performed, each held this many times with neutral between. */
  performed: readonly GestureCategory[]
  holdsPerPose: number
}

const RECORDINGS: readonly Recording[] = [
  {
    file: 'webcam-session-1.jsonl',
    jsonl: webcamSession1,
    performed: ['Closed_Fist', 'Thumb_Down', 'Thumb_Up', 'Open_Palm'],
    holdsPerPose: 3,
  },
]

interface Acceptance {
  t: number
  pair: GesturePair
  /** The first hand of the frame that was accepted, as the recognizer reported it. */
  hand: RecordedHand | undefined
}

function replay(recording: GestureRecording, config: GesturePolicyConfig): Acceptance[] {
  let state = createGesturePolicyState()
  const acceptances: Acceptance[] = []
  for (const entry of recording.entries) {
    if (entry.kind !== 'recognizer') continue
    const hand = entry.hands[0]
    const step = stepGesturePolicy(state, observeHand(entry.t, hand), config)
    state = step.state
    if (step.outcome.kind === 'accepted') acceptances.push({ t: entry.t, pair: step.outcome.pair, hand })
  }
  return acceptances
}

function countByGesture(acceptances: Acceptance[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const { pair } of acceptances) counts[pair.gesture] = (counts[pair.gesture] ?? 0) + 1
  return counts
}

/** Accepted outcomes the live loop recorded into the session, under the rule in force then. */
function countRecordedAccepted(recording: GestureRecording): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const entry of recording.entries) {
    if (entry.kind !== 'policy' || entry.outcome.kind !== 'accepted') continue
    const gesture = entry.outcome.gesture ?? 'unknown'
    counts[gesture] = (counts[gesture] ?? 0) + 1
  }
  return counts
}

function scoresFor(recording: GestureRecording, category: GestureCategory): number[] {
  const scores: number[] = []
  for (const entry of recording.entries) {
    if (entry.kind !== 'recognizer') continue
    const hand = entry.hands[0]
    if (hand && hand.category === category) scores.push(hand.score)
  }
  return scores
}

const median = (values: number[]): number => {
  if (values.length === 0) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 1 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2
}

const share = (values: number[], threshold: number): number =>
  values.length === 0 ? 0 : Math.round((values.filter((value) => value >= threshold).length / values.length) * 100)

const describeCounts = (counts: Record<string, number>): string =>
  Object.entries(counts)
    .map(([gesture, count]) => `${gesture} ${count}`)
    .join(', ') || 'none'

function pairFor(gesture: GestureCategory): GesturePair {
  const pair = DEFAULT_GESTURE_PAIRS.find((item) => item.gesture === gesture)
  if (!pair) throw new Error(`${gesture} is not in the default pairs`)
  return pair
}

const OLD_CONFIG: GesturePolicyConfig = {
  ...DEFAULT_GESTURE_POLICY_CONFIG,
  pairs: DEFAULT_GESTURE_PAIRS.map((pair) => ({ ...pair, minScore: OLD_FIXED_MIN_SCORE })),
}

for (const { file, jsonl, performed, holdsPerPose } of RECORDINGS) {
  const recording = parseGestureRecording(jsonl)
  const tuned = replay(recording, DEFAULT_GESTURE_POLICY_CONFIG)
  const tunedCounts = countByGesture(tuned)
  const oldCounts = countByGesture(replay(recording, OLD_CONFIG))
  const recordedCounts = countRecordedAccepted(recording)

  describe(`${file} replayed through the default pairs`, () => {
    test('is the stripped download: a header comment naming the source, every recognizer hand without landmarks', () => {
      expect(recording.header).toMatchObject({ kind: 'header', v: 1, source: 'webcam' })
      expect(recording.header.comment).toMatch(/landmarks are stripped/i)
      const frames = recording.entries.filter((entry) => entry.kind === 'recognizer')
      expect(frames.length).toBeGreaterThan(0)
      for (const entry of frames) {
        if (entry.kind !== 'recognizer') continue
        for (const hand of entry.hands) {
          expect(hand).not.toHaveProperty('landmarks')
          expect(Object.keys(hand).sort()).toEqual(['category', 'handedness', 'rawCategory', 'score'])
        }
      }
      for (const kind of ['policy', 'status', 'intent']) {
        expect(recording.entries.some((entry) => entry.kind === kind)).toBe(true)
      }
    })

    for (const gesture of performed) {
      const pair = pairFor(gesture)
      const scores = scoresFor(recording, gesture)
      const acceptedCount = tunedCounts[gesture] ?? 0
      const distribution = `n=${scores.length}, median ${median(scores).toFixed(2)}, ${share(scores, pair.minScore)}% at or above minScore ${pair.minScore.toFixed(2)}, ${share(scores, OLD_FIXED_MIN_SCORE)}% at or above ${OLD_FIXED_MIN_SCORE.toFixed(2)}`
      test(`${gesture} accepted ${acceptedCount} times in replay (${distribution}; ${holdsPerPose} holds recorded)`, () => {
        expect(acceptedCount).toBeGreaterThanOrEqual(holdsPerPose)
      })
    }

    test(`accepted outcomes per pose: ${describeCounts(tunedCounts)}; none while the recognizer reports None or a pose outside the recording's pairs`, () => {
      for (const { pair, hand } of tuned) {
        expect(hand).toBeDefined()
        expect(hand?.category).toBe(pair.gesture)
        expect(hand?.rawCategory).toBe(pair.gesture)
      }
      const recordedPairs = new Set(recording.header.pairs.map((pair) => pair.gesture))
      for (const gesture of GESTURE_CATEGORIES) {
        if (performed.includes(gesture)) continue
        expect(tunedCounts[gesture] ?? 0).toBe(0)
      }
      for (const gesture of Object.keys(tunedCounts)) {
        expect(recordedPairs.has(gesture)).toBe(true)
        expect(performed).toContain(gesture)
      }
    })

    test(`at the old fixed ${OLD_FIXED_MIN_SCORE.toFixed(2)} threshold the replay accepts ${describeCounts(oldCounts)} and the live loop recorded ${describeCounts(recordedCounts)}`, () => {
      // The fixture proves the change: Thumb_Up and Open_Palm never reached 0.80 on any frame.
      expect(recording.header.pairs.every((pair) => pair.minScore === OLD_FIXED_MIN_SCORE)).toBe(true)
      expect(oldCounts['Thumb_Up'] ?? 0).toBe(0)
      expect(oldCounts['Open_Palm'] ?? 0).toBe(0)
      expect(recordedCounts['Thumb_Up'] ?? 0).toBe(0)
      expect(recordedCounts['Open_Palm'] ?? 0).toBe(0)
      // and the replay harness reproduces what the live loop recorded under that threshold.
      expect(oldCounts).toEqual(recordedCounts)
      expect(share(scoresFor(recording, 'Thumb_Up'), OLD_FIXED_MIN_SCORE)).toBe(0)
      expect(share(scoresFor(recording, 'Open_Palm'), OLD_FIXED_MIN_SCORE)).toBe(0)
    })
  })
}
