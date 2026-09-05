import type {
  CapturePattern,
  CaptureRoomArgs,
  ConsoleIntentName,
  DroneId,
  IntentArgs,
  IntentArgsByName,
  IntentSource,
  IntentV1,
  TranslateArgs,
} from '../relay/contract'

export interface IntentFactoryDependencies {
  now: () => number
  nextId: () => string
}

export interface IntentDraftInput<N extends ConsoleIntentName = ConsoleIntentName> {
  name: N
  args: IntentArgsByName[N]
  selection: DroneId[]
  source: IntentSource
  session: string
  confirm?: boolean
  retryOf?: string | null
}

export const browserIntentDependencies: IntentFactoryDependencies = {
  now: () => Date.now(),
  nextId: () => crypto.randomUUID(),
}

/**
 * Every request starts here and keeps this intent_id through its whole
 * lifecycle; confirmation only restamps t and sets confirm true.
 */
export function createIntent<N extends ConsoleIntentName>(
  input: IntentDraftInput<N>,
  dependencies: IntentFactoryDependencies = browserIntentDependencies,
): IntentV1 {
  return {
    v: 1,
    t: dependencies.now(),
    type: 'intent',
    intent_id: dependencies.nextId(),
    retry_of: input.retryOf ?? null,
    source: input.source,
    session: input.session,
    name: input.name,
    args: cloneArgs(input.args),
    selection: [...input.selection],
    mode: 'indoor',
    confirm: input.confirm ?? false,
  }
}

export function confirmIntent(intent: IntentV1, confirmedAt: number): IntentV1 {
  return { ...intent, t: confirmedAt, confirm: true }
}

/** Room identifiers: lower-case letters, digits and hyphens, 3 to 24 characters. */
export const ROOM_ID_PATTERN = /^[a-z0-9][a-z0-9-]{2,23}$/

export function isValidRoomId(roomId: string): boolean {
  return ROOM_ID_PATTERN.test(roomId)
}

/** The capture id is minted from the intent id at draft time, so it is unique per request. */
export function createCaptureArgs(
  roomId: string,
  intentId: string,
  pattern: CapturePattern,
): CaptureRoomArgs {
  return {
    room_id: roomId,
    capture_id: `capture-${intentId}`,
    pattern,
  }
}

export type TranslateDirection = 'north' | 'south' | 'east' | 'west'

const UNIT_VECTORS: Record<TranslateDirection, TranslateArgs> = {
  north: { dx: 0, dy: 1 },
  south: { dx: 0, dy: -1 },
  east: { dx: 1, dy: 0 },
  west: { dx: -1, dy: 0 },
}

export const TRANSLATE_STEPS_MIN = 1
export const TRANSLATE_STEPS_MAX = 6

export function clampTranslateSteps(value: number): number {
  if (!Number.isFinite(value)) return TRANSLATE_STEPS_MIN
  return Math.max(TRANSLATE_STEPS_MIN, Math.min(TRANSLATE_STEPS_MAX, Math.round(value)))
}

/** Unit vector for the direction times the step count, in the room frame. */
export function createTranslateArgs(direction: TranslateDirection, steps: number): TranslateArgs {
  const unit = UNIT_VECTORS[direction]
  const n = clampTranslateSteps(steps)
  return { dx: unit.dx * n, dy: unit.dy * n }
}

/**
 * A retry mints a new intent id, points retry_of at the original, and carries
 * the rest of the envelope over unchanged: args, selection and confirm. A
 * confirmation-gated request only reaches failed or refused after the operator
 * confirmed it, so the retry keeps that confirmation and sends at once rather
 * than opening a second preview; the relay refuses capture_room without it.
 */
export function retryIntent(
  failed: IntentV1,
  dependencies: IntentFactoryDependencies = browserIntentDependencies,
): IntentV1 {
  return {
    ...failed,
    t: dependencies.now(),
    intent_id: dependencies.nextId(),
    retry_of: failed.intent_id,
    args: cloneArgs(failed.args),
    selection: [...failed.selection],
    confirm: failed.confirm,
  }
}

function cloneArgs<A extends IntentArgs>(args: A): A {
  if ('ids' in args) return { ...args, ids: [...args.ids] }
  if ('box' in args) return { ...args, box: { ...args.box } }
  return { ...args }
}
