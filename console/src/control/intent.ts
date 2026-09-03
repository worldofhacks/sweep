import type {
  CapturePattern,
  ConsoleIntentName,
  DroneId,
  IntentArgs,
  IntentSource,
  IntentV1,
} from '../relay/contract'

export interface IntentFactoryDependencies {
  now: () => number
  nextId: () => string
}

export interface IntentDraftInput {
  name: ConsoleIntentName
  args: IntentArgs
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

export function createIntent(
  input: IntentDraftInput,
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
    args: input.args,
    selection: [...input.selection],
    mode: 'indoor',
    confirm: input.confirm ?? false,
  }
}

export function confirmIntent(intent: IntentV1, confirmedAt: number): IntentV1 {
  return { ...intent, t: confirmedAt, confirm: true }
}

export function createCaptureArgs(
  roomId: string,
  intentId: string,
  pattern: CapturePattern,
): IntentArgs {
  return {
    room_id: roomId,
    capture_id: `capture-${intentId}`,
    pattern,
  }
}

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
    confirm: false,
  }
}

function cloneArgs(args: IntentArgs): IntentArgs {
  if ('ids' in args) return { ids: [...args.ids] }
  if ('room_id' in args) return { ...args }
  if ('delta' in args) return { ...args }
  if ('name' in args) return { ...args }
  return {}
}
