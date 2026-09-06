/**
 * JSONL session recorder for the gesture producer: recognizer outputs, policy
 * transitions, producer status changes, and every intent-level event, each
 * stamped with monotonic and wall-clock time. The output feeds the recorded
 * gesture sessions used by the eval fixtures.
 */
import type { IntentV1 } from '../relay/contract'
import type { CameraStatus } from './camera'
import type { HandObservation, RecognizerStatus } from './recognizer'
import type { GesturePair, GesturePhase, GesturePolicyOutcome } from './policy'

export const GESTURE_SESSION_RECORD_VERSION = 1

export interface RecordedOutcome {
  kind: GesturePolicyOutcome['kind']
  gesture?: string
  action?: string
  heldMs?: number
  progress?: number
  score?: number
  reason?: string
  category?: string
  neutralMs?: number
}

export type GestureIntentEvent =
  | 'draft'
  | 'confirm'
  | 'cancel'
  | 'blocked'

export type SessionRecordInput =
  | { kind: 'recognizer'; t: number; hands: HandObservation[] }
  | { kind: 'policy'; t: number; phase: GesturePhase; outcome: GesturePolicyOutcome }
  | {
      kind: 'status'
      t: number
      enabled: boolean
      camera: CameraStatus
      recognizer: RecognizerStatus
      detail: string | null
    }
  | {
      kind: 'intent'
      t: number
      event: GestureIntentEvent
      intent_id: string | null
      name: string | null
      detail: string | null
      intent?: IntentV1
    }

export type SessionRecordEntry = (
  | { kind: 'recognizer'; hands: HandObservation[] }
  | { kind: 'policy'; phase: GesturePhase; outcome: RecordedOutcome }
  | {
      kind: 'status'
      enabled: boolean
      camera: CameraStatus
      recognizer: RecognizerStatus
      detail: string | null
    }
  | {
      kind: 'intent'
      event: GestureIntentEvent
      intent_id: string | null
      name: string | null
      detail: string | null
      intent?: IntentV1
    }
) & { seq: number; t: number; wall_t: number }

export interface SessionRecordHeader {
  kind: 'header'
  v: typeof GESTURE_SESSION_RECORD_VERSION
  session: string
  source: 'webcam'
  wall_t: number
  pairs: Array<{ gesture: string; action: string; minScore: number; dwellMs: number }>
}

export interface SessionRecorder {
  record(input: SessionRecordInput): void
  entries(): readonly SessionRecordEntry[]
  readonly size: number
  readonly dropped: number
  toJsonl(): string
  clear(): void
}

export interface SessionRecorderOptions {
  sessionId: string
  pairs: readonly GesturePair[]
  wallClock: () => number
  /** Oldest entries are dropped past this many; the count is reported. */
  capacity?: number
}

export const DEFAULT_RECORDER_CAPACITY = 30_000

class BoundedSessionRecorder implements SessionRecorder {
  private items: SessionRecordEntry[] = []
  private sequence = 0
  private droppedCount = 0
  private readonly options: Required<SessionRecorderOptions>

  constructor(options: SessionRecorderOptions) {
    this.options = { capacity: DEFAULT_RECORDER_CAPACITY, ...options }
  }

  get size(): number {
    return this.items.length
  }

  get dropped(): number {
    return this.droppedCount
  }

  record(input: SessionRecordInput): void {
    this.sequence += 1
    const stamp = { seq: this.sequence, t: input.t, wall_t: this.options.wallClock() }
    let entry: SessionRecordEntry
    switch (input.kind) {
      case 'recognizer':
        entry = { ...stamp, kind: 'recognizer', hands: input.hands.map(compactHand) }
        break
      case 'policy':
        entry = { ...stamp, kind: 'policy', phase: input.phase, outcome: summarizeOutcome(input.outcome) }
        break
      case 'status':
        entry = {
          ...stamp,
          kind: 'status',
          enabled: input.enabled,
          camera: input.camera,
          recognizer: input.recognizer,
          detail: input.detail,
        }
        break
      case 'intent':
        entry = {
          ...stamp,
          kind: 'intent',
          event: input.event,
          intent_id: input.intent_id,
          name: input.name,
          detail: input.detail,
          ...(input.intent ? { intent: input.intent } : {}),
        }
        break
    }
    this.items.push(entry)
    if (this.items.length > this.options.capacity) {
      this.items.splice(0, this.items.length - this.options.capacity)
      this.droppedCount += 1
    }
  }

  entries(): readonly SessionRecordEntry[] {
    return this.items
  }

  toJsonl(): string {
    const header: SessionRecordHeader = {
      kind: 'header',
      v: GESTURE_SESSION_RECORD_VERSION,
      session: this.options.sessionId,
      source: 'webcam',
      wall_t: this.options.wallClock(),
      pairs: this.options.pairs.map((pair) => ({
        gesture: pair.gesture,
        action: describeAction(pair),
        minScore: pair.minScore,
        dwellMs: pair.dwellMs,
      })),
    }
    return [header, ...this.items].map((line) => JSON.stringify(line)).join('\n') + '\n'
  }

  clear(): void {
    this.items = []
    this.sequence = 0
    this.droppedCount = 0
  }
}

export function createSessionRecorder(options: SessionRecorderOptions): SessionRecorder {
  return new BoundedSessionRecorder(options)
}

export function summarizeOutcome(outcome: GesturePolicyOutcome): RecordedOutcome {
  switch (outcome.kind) {
    case 'idle':
      return { kind: 'idle' }
    case 'unmapped':
      return { kind: 'unmapped', category: outcome.category }
    case 'low_confidence':
      return { kind: 'low_confidence', ...pairFields(outcome.pair), score: round(outcome.score) }
    case 'candidate':
      return {
        kind: 'candidate',
        ...pairFields(outcome.pair),
        heldMs: outcome.heldMs,
        progress: round(outcome.progress),
      }
    case 'accepted':
      return { kind: 'accepted', ...pairFields(outcome.pair), heldMs: outcome.heldMs }
    case 'dwell_timeout':
      return {
        kind: 'dwell_timeout',
        ...pairFields(outcome.pair),
        heldMs: outcome.heldMs,
        reason: outcome.reason,
      }
    case 'duplicate_suppressed':
      return { kind: 'duplicate_suppressed', ...pairFields(outcome.pair), category: outcome.category }
    case 'wait_for_release':
      return { kind: 'wait_for_release', ...pairFields(outcome.pair), neutralMs: outcome.neutralMs }
    case 'released':
      return { kind: 'released', ...pairFields(outcome.pair) }
  }
}

function pairFields(pair: GesturePair): { gesture: string; action: string } {
  return { gesture: pair.gesture, action: describeAction(pair) }
}

function describeAction(pair: GesturePair): string {
  if (pair.action.kind === 'draft' && pair.action.name === 'body_pulse') return `draft:body_pulse:${pair.action.direction}`
  return pair.action.kind === 'draft' ? `draft:${pair.action.name}` : pair.action.kind
}

function compactHand(hand: HandObservation): HandObservation {
  return {
    ...hand,
    score: round(hand.score),
    landmarks: hand.landmarks.map(({ x, y, z }) => ({ x: round(x), y: round(y), z: round(z) })),
  }
}

function round(value: number): number {
  return Math.round(value * 10_000) / 10_000
}
