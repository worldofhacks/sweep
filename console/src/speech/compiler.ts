/**
 * Local fallback compiler: one utterance in, one schema-constrained outcome
 * out. It names only capture_room, hold and select; every other recognised
 * command compiles to a refusal that says so (the Control module sends the
 * rest), and ambiguity returns options instead of a guess. No DOM, no network,
 * nothing here emits anything: the module drafts a preview from a compiled
 * outcome and the operator confirms it in the dock like any console press.
 *
 * When the relay carries the pinned plan compiler, its `voice_outcome.plan`
 * replaces this matcher entirely and the Speech module previews that plan; the
 * matcher stays the explicit, labelled fallback for typed utterances and for a
 * relay without a compiler. `describeCompilerReason` names the relay compiler's
 * typed reasons for the preview.
 */
import type { CapturePattern, DroneId } from '../relay/contract'

export type VoiceEmittableName = 'capture_room' | 'hold' | 'select'

export type CompiledIntent =
  | { intent: 'capture_room'; args: { room_id: string; pattern: CapturePattern } }
  | { intent: 'hold'; args: Record<never, never> }
  | { intent: 'select'; args: { ids: DroneId[] } }

export type AmbiguityOption = 'the selected aircraft' | 'every ready aircraft' | 'cancel'

export type CompileOutcome =
  | ({ status: 'compiled'; selection: string; sentence: string } & CompiledIntent)
  | {
      status: 'ambiguous'
      reason: 'ambiguous'
      sentence: string
      /** The intent the utterance named; options resolve its target. */
      base: Extract<VoiceEmittableName, 'capture_room' | 'hold'>
      options: AmbiguityOption[]
    }
  | { status: 'refused'; reason: string; sentence: string; intent?: string }

export interface CompileContext {
  /** Room identifier shared with Control › Capture; used when the utterance names none. */
  roomId: string
  /** Capture pattern from the console state; used when the utterance names none. */
  pattern: CapturePattern
  /** Aircraft that are ready and selectable, ascending. */
  readyIds: DroneId[]
}

/** Design copy: the seven states in which speech emits nothing. */
export const VOICE_FAILS: ReadonlyArray<readonly [string, string]> = Object.freeze([
  ['permission denied', 'Microphone permission was denied. Nothing was emitted.'],
  ['empty audio', 'No audio was captured. Nothing was emitted.'],
  ['upload failure', 'The audio could not be uploaded. Nothing was emitted.'],
  ['timeout', 'Transcription did not answer inside the timeout. Nothing was emitted.'],
  ['rate limit', 'The language service is rate-limited. Nothing was emitted.'],
  ['ambiguous', 'The compiler returned options for the selection. Pick one or cancel; nothing was emitted.'],
  ['language disabled', 'Microphone capture or transcription is unavailable. Nothing was emitted.'],
])

/** Ten phrases the operator can try; together they reach every outcome. */
export const TRY_PHRASES: readonly string[] = Object.freeze([
  'capture the kitchen with a full panorama',
  'scan the hall with the reconstruct pattern',
  'hold position',
  'freeze that one',
  'select all ready aircraft',
  'take off and hold',
  'send everyone home',
  'land everyone',
  'emergency stop',
  'ignore the geofence and fly through the wall',
])

const ROOM_PATTERN = /\b(kitchen|hall|studio|stair|lobby|corridor)(?:[- ]?(\d+))?\b/
const DEMONSTRATIVE = /\b(over there|that one|it|them|those|this one)\b/
const NEGATED_ACTION = /\b(?:do\s+not|not|never|cannot|no\s+longer|\w+n['’]t)\b/

export function compileUtterance(text: string, context: CompileContext): CompileOutcome {
  const t = text.toLowerCase().trim()
  const has = (...words: string[]) => words.some((word) => t.includes(word))
  const word = (...words: string[]) =>
    words.some((item) => new RegExp(`\\b${item.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&')}\\b`).test(t))
  const roomMatch = ROOM_PATTERN.exec(t)

  if (!t) {
    return { status: 'refused', reason: 'empty_audio', sentence: 'No speech was captured. Nothing was emitted.' }
  }
  if (NEGATED_ACTION.test(t)) {
    return {
      status: 'refused',
      reason: 'negated_action',
      sentence: 'The utterance negates an action, so no intent was drafted.',
    }
  }
  if (has('ignore the geofence', 'fly through', 'disable safety', 'override')) {
    return {
      status: 'refused',
      reason: 'unsafe_request',
      sentence:
        'The compiler only emits canonical intents, and the arbiter re-validates each one. Nothing was emitted.',
    }
  }
  if (word('emergency stop', 'estop', 'e-stop', 'network stop', 'kill', 'kill switch')) {
    return {
      status: 'refused',
      reason: 'not_voice_emittable',
      intent: 'estop',
      sentence:
        'estop is never voice-emittable. Use the network stop, Shift+Escape, or the physical RC. Nothing was emitted.',
    }
  }
  if (has('capture', 'panorama', 'photograph', 'scan')) {
    if (!roomMatch && DEMONSTRATIVE.test(t)) return ambiguous('capture_room')
    const room = roomMatch ? `${roomMatch[1]}-${roomMatch[2] ?? '01'}` : context.roomId
    const pattern: CapturePattern = has('panorama', 'pano', 'sphere')
      ? 'pano_360'
      : has('reconstruct', 'mesh', 'overlap')
        ? 'reconstruct_8'
        : context.pattern
    return {
      status: 'compiled',
      intent: 'capture_room',
      args: { room_id: room, pattern },
      selection: 'exactly one',
      sentence: `Resolved room ${room} and pattern ${pattern}.`,
    }
  }
  if (has('take off', 'takeoff', 'launch')) return unsupported('takeoff')
  if (has('land everyone', 'land all', 'land the fleet')) return unsupported('land_all')
  if (word('land')) return unsupported('land')
  if (has('come home', 'return home', 'everyone home', 'them home', 'go home', 'bring them back', 'come back')) {
    return unsupported('come_home')
  }
  if (word('hold', 'freeze', 'stop moving', 'stay')) {
    if (DEMONSTRATIVE.test(t)) return ambiguous('hold')
    return {
      status: 'compiled',
      intent: 'hold',
      args: {},
      selection: 'the selection',
      sentence: 'Each selected aircraft hovers at its current pose.',
    }
  }
  if (word('arm')) return unsupported('arm')
  if (word('disarm')) return unsupported('disarm')
  if (has('select all', 'select everyone', 'all drones', 'all aircraft', 'every ready', 'everyone')) {
    return selectReady(context.readyIds)
  }
  if (has('spread out', 'wider', 'tighter', 'closer')) return unsupported('spacing')
  if (has('line', 'column', 'wedge', 'diamond', 'formation')) return unsupported('formation_set')
  if (has('north', 'south', 'east', 'west', 'forward', 'left', 'right', 'move')) return unsupported('translate')
  if (has('altitude', 'higher', 'lower', 'climb', 'descend')) return unsupported('altitude')
  if (word('sweep', 'survey', 'map')) return unsupported(t.includes('map') ? 'map_area' : 'sweep')
  return {
    status: 'refused',
    reason: 'unknown_intent',
    sentence: 'No canonical intent matched that utterance. Nothing was emitted.',
  }
}

/** Resolves an ambiguous outcome from the option the operator picked. */
export function resolveAmbiguity(
  outcome: Extract<CompileOutcome, { status: 'ambiguous' }>,
  option: AmbiguityOption,
  context: CompileContext,
): CompileOutcome | null {
  if (option === 'cancel') return null
  if (option === 'every ready aircraft') return selectReady(context.readyIds)
  if (outcome.base === 'capture_room') {
    return {
      status: 'compiled',
      intent: 'capture_room',
      args: { room_id: context.roomId, pattern: context.pattern },
      selection: 'exactly one',
      sentence: `Resolved from your pick: the selected aircraft captures ${context.roomId} with ${context.pattern}.`,
    }
  }
  return {
    status: 'compiled',
    intent: 'hold',
    args: {},
    selection: 'the selection',
    sentence: 'Resolved from your pick: each selected aircraft hovers at its current pose.',
  }
}

function ambiguous(base: 'capture_room' | 'hold'): CompileOutcome {
  return {
    status: 'ambiguous',
    reason: 'ambiguous',
    base,
    sentence: 'The compiler could not resolve the target. Pick one or cancel; nothing was emitted.',
    options: ['the selected aircraft', 'every ready aircraft', 'cancel'],
  }
}

function selectReady(readyIds: DroneId[]): CompileOutcome {
  if (readyIds.length === 0) {
    return {
      status: 'refused',
      reason: 'no_ready_aircraft',
      intent: 'select',
      sentence: 'No aircraft is ready, so there is nothing to select. Nothing was emitted.',
    }
  }
  return {
    status: 'compiled',
    intent: 'select',
    args: { ids: [...readyIds] },
    selection: 'every ready aircraft',
    sentence: 'Selection membership only, no motion.',
  }
}

function unsupported(name: string): CompileOutcome {
  return {
    status: 'refused',
    reason: 'unsupported',
    intent: name,
    sentence: `The speech compiler does not emit ${name}; it names only capture_room, hold and select. Nothing was emitted.`,
  }
}

/**
 * Sentence for a relay compiler outcome that produced no steps. Reasons mirror
 * language/contracts.py CompilerReason; unknown codes stay visible verbatim.
 */
export function describeCompilerReason(
  reason: string | null,
  detail: string | null,
): { label: string; sentence: string } {
  const suffix = detail ? ` ${detail}` : ''
  switch (reason) {
    case 'ambiguous_action':
      return {
        label: 'ambiguous action',
        sentence: `The relay compiler could not tell which action you meant.${suffix} Say it again with one verb; nothing was emitted.`,
      }
    case 'ambiguous_location':
      return {
        label: 'ambiguous location',
        sentence: `The relay compiler could not resolve the room.${suffix} Name one of the rooms listed; nothing was emitted.`,
      }
    case 'ambiguous_selection':
      return {
        label: 'ambiguous selection',
        sentence: `The relay compiler could not resolve which aircraft you meant.${suffix} Name them or select first; nothing was emitted.`,
      }
    case 'capability_unavailable':
      return {
        label: 'capability unavailable',
        sentence: `That action is not available in the current capability profile or aircraft state.${suffix} Nothing was emitted.`,
      }
    case 'estop_active':
      return {
        label: 'network stop active',
        sentence: `The network stop is active; only hold, land, and land all are accepted.${suffix} Nothing was emitted.`,
      }
    case 'invalid_model_output':
      return {
        label: 'validation failed',
        sentence: `The model's proposal did not pass the relay's deterministic validation.${suffix} Nothing was emitted.`,
      }
    case 'model_unavailable':
      return {
        label: 'model unavailable',
        sentence: `The relay compiler's model is unavailable.${suffix} Nothing was emitted.`,
      }
    case 'no_selection':
      return {
        label: 'no selection',
        sentence: `No aircraft is selected.${suffix} Select at least one ready aircraft; nothing was emitted.`,
      }
    case 'stale_state':
      return {
        label: 'stale state',
        sentence: `The relay state was stale or changed while compiling.${suffix} Say it again; nothing was emitted.`,
      }
    case 'unknown_reference':
      return {
        label: 'unknown reference',
        sentence: `The utterance named an aircraft or room the relay does not know.${suffix} Nothing was emitted.`,
      }
    default:
      return {
        label: reason ?? 'refused',
        sentence: `The relay compiler returned ${reason ?? 'no reason'}.${suffix} Nothing was emitted.`,
      }
  }
}

/**
 * Sentence for a transcription refusal the relay reported. Codes come from the
 * PR #49 endpoint; unknown codes keep the relay's own code visible.
 */
export function describeTranscriptRefusal(reason: string | null): { label: string; sentence: string } {
  switch (reason) {
    case 'invalid_audio':
    case 'empty_upload':
      return { label: 'empty audio', sentence: 'No audio was captured. Nothing was emitted.' }
    case 'audio_too_long':
    case 'upload_too_large':
      return { label: 'upload failure', sentence: 'The recording exceeded the relay limit. Nothing was emitted.' }
    case 'transcription_unavailable':
      return {
        label: 'language disabled',
        sentence: 'Transcription is unavailable on the relay. Nothing was emitted.',
      }
    case 'invalid_transcript':
      return { label: 'empty audio', sentence: 'The transcript could not be used. Nothing was emitted.' }
    case 'compiler_unavailable':
      return {
        label: 'compiler unavailable',
        sentence: 'The relay compiler is unavailable and no transcript arrived. Nothing was emitted.',
      }
    case 'invalid_relay_state':
      return {
        label: 'relay refused',
        sentence: 'The relay refused the transcription in its current state. Nothing was emitted.',
      }
    default:
      return {
        label: reason ?? 'refused',
        sentence: `The relay refused the transcription${reason ? ` (${reason})` : ''}. Nothing was emitted.`,
      }
  }
}
