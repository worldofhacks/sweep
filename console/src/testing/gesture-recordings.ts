/**
 * Recorded gesture sessions for the policy eval (`src/gesture/policy.eval.test.ts`).
 * A recording is the Gesture module's Download session (JSONL) with the hand
 * landmarks stripped from every recognizer frame so it stays small enough to
 * keep under `src/testing/gesture-recordings/`; every other field is verbatim
 * and the header carries a `comment` naming the source and the stripping.
 * `parseGestureRecording` reads both the stripped and the raw form.
 */
import type { GestureCategory } from '../gesture/policy'
import type { HandLandmark } from '../gesture/recognizer'
import type { SessionRecordEntry, SessionRecordHeader } from '../gesture/recorder'

export interface RecordedHand {
  category: GestureCategory | null
  rawCategory: string | null
  score: number
  handedness: string | null
  /** Present in a raw download, absent once stripped. */
  landmarks?: HandLandmark[]
}

export type RecordedEntry =
  | (Omit<Extract<SessionRecordEntry, { kind: 'recognizer' }>, 'hands'> & { hands: RecordedHand[] })
  | Exclude<SessionRecordEntry, { kind: 'recognizer' }>

export interface RecordedHeader extends SessionRecordHeader {
  comment?: string
}

export interface GestureRecording {
  header: RecordedHeader
  entries: RecordedEntry[]
}

export function parseGestureRecording(jsonl: string): GestureRecording {
  const lines = jsonl.split('\n').filter((line) => line.trim() !== '')
  if (lines.length === 0) throw new Error('The recording is empty.')
  const header = JSON.parse(lines[0]) as RecordedHeader
  if (header.kind !== 'header') throw new Error('The recording does not start with a header line.')
  const entries = lines.slice(1).map((line) => JSON.parse(line) as RecordedEntry)
  return { header, entries }
}

/**
 * The stripped form: landmarks removed from every recognizer hand, the header
 * stamped with `comment`, everything else verbatim and in its original order.
 */
export function stripGestureRecording(jsonl: string, comment: string): string {
  const { header, entries } = parseGestureRecording(jsonl)
  const stripped = entries.map((entry) =>
    entry.kind === 'recognizer'
      ? {
          ...entry,
          hands: entry.hands.map(({ category, rawCategory, score, handedness }) => ({
            category,
            rawCategory,
            score,
            handedness,
          })),
        }
      : entry,
  )
  const { kind, ...rest } = header
  return [{ kind, comment, ...rest }, ...stripped].map((line) => JSON.stringify(line)).join('\n') + '\n'
}
