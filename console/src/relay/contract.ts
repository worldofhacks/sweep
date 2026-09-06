/**
 * Console-side mirror of the frozen Intent v1 contract in relay/intent_v1.py.
 *
 * Relay event envelopes are deliberately kept in this one module while M1.1 is
 * integrated. Components and reducers consume these normalized shapes and do
 * not infer transport, planner, or safety semantics.
 */

export type DroneId = number
export type CapturePattern = 'pano_360' | 'reconstruct_8'
export type IntentSource = 'console' | 'keyboard' | 'webcam' | 'language'
export type FormationName = 'line' | 'column' | 'circle' | 'grid' | 'V'

// Intent producer ceilings mirrored from relay/intent_v1.py. JavaScript numbers
// use their exact integer ceiling; the relay additionally accepts signed-Long
// timestamps from non-JavaScript peers.
export const MAX_INTENT_IDENTIFIER_CODE_POINTS = 128
export const MAX_INTENT_SESSION_CODE_POINTS = 512
export const MAX_INTENT_SOURCE_CODE_POINTS = 64
export const MAX_INTENT_NAME_CODE_POINTS = 64
export const MAX_INTENT_DRONE_IDS = 6
export const MAX_INTENT_DRONE_ID = 2_147_483_647

/**
 * Every intent name this console can build. Mirrors relay/intent_v1.py
 * IntentName minus survey_area and map_area, which the brief marks as later.
 */
export type ConsoleIntentName =
  | 'arm'
  | 'disarm'
  | 'estop'
  | 'select'
  | 'takeoff'
  | 'land'
  | 'land_all'
  | 'hold'
  | 'translate'
  | 'altitude'
  | 'formation_next'
  | 'formation_set'
  | 'spacing'
  | 'come_home'
  | 'sweep'
  | 'capture_room'

export const CONSOLE_INTENT_NAMES: readonly ConsoleIntentName[] = [
  'arm',
  'disarm',
  'estop',
  'select',
  'takeoff',
  'land',
  'land_all',
  'hold',
  'translate',
  'altitude',
  'formation_next',
  'formation_set',
  'spacing',
  'come_home',
  'sweep',
  'capture_room',
]

/**
 * Mirror of the relay's implemented names, including the earned M1.5 simulator
 * behaviors. Other names remain visible but are disabled by the authoritative
 * advertised capability profile.
 */
export const SUPPORTED_INTENTS: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'arm',
  'select',
  'takeoff',
  'translate',
  'hold',
  'come_home',
  'land',
  'land_all',
  'estop',
  'capture_room',
  'altitude',
  'formation_next',
  'formation_set',
  'spacing',
  'sweep',
])

/** The exact profile emitted by the current C1 relay. */
export const C1_BASIC_CONTROL_INTENTS: readonly ConsoleIntentName[] = [
  'arm',
  'altitude',
  'capture_room',
  'come_home',
  'estop',
  'formation_next',
  'formation_set',
  'hold',
  'land',
  'land_all',
  'select',
  'spacing',
  'sweep',
  'takeoff',
  'translate',
]

export function isSupportedIntent(name: ConsoleIntentName): boolean {
  return SUPPORTED_INTENTS.has(name)
}

/**
 * Console policy from the design brief: these intents never leave the console
 * without the operator confirming the exact envelope. The relay itself only
 * enforces confirmation for capture_room.
 */
export const CONFIRM_REQUIRED_INTENTS: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'takeoff',
  'land',
  'land_all',
  'sweep',
  'capture_room',
])

export function requiresConfirmation(name: ConsoleIntentName): boolean {
  return CONFIRM_REQUIRED_INTENTS.has(name)
}

/** Selection rule per intent, from the brief's controls table. */
export type SelectionRule = 'any' | 'at least one' | 'selected' | 'all' | 'exactly one' | 'fleet'

export const SELECTION_RULES: Readonly<Record<ConsoleIntentName, SelectionRule>> = {
  arm: 'any',
  disarm: 'any',
  estop: 'fleet',
  select: 'at least one',
  takeoff: 'selected',
  land: 'selected',
  land_all: 'all',
  hold: 'selected',
  translate: 'selected',
  altitude: 'selected',
  formation_next: 'selected',
  formation_set: 'selected',
  spacing: 'selected',
  come_home: 'selected',
  sweep: 'selected',
  capture_room: 'exactly one',
}

export function selectionRule(name: ConsoleIntentName): SelectionRule {
  return SELECTION_RULES[name]
}

/**
 * True when the intent's selection is the authoritative selection, so a change
 * to that selection invalidates a pending preview. `all` and `fleet` intents
 * address the roster instead and are invalidated by roster changes only.
 */
export function followsSelection(name: ConsoleIntentName): boolean {
  const rule = SELECTION_RULES[name]
  return rule === 'selected' || rule === 'exactly one' || rule === 'at least one'
}

export type MembershipState =
  | 'registered'
  | 'ready'
  | 'leaving'
  | 'disconnected'
  | 'degraded'

export type EmptyArgs = Record<string, never>
export interface SelectArgs {
  ids: DroneId[]
}
export interface TranslateArgs {
  /** Steps in the room frame, east positive. */
  dx: number
  /** Steps in the room frame, north positive. */
  dy: number
}
export interface DeltaArgs {
  delta: number
}
export interface FormationSetArgs {
  name: FormationName
}
export interface SweepBox {
  min_x: number
  max_x: number
  min_y: number
  max_y: number
}
export type SweepArgs = EmptyArgs | { box: SweepBox }
export interface CaptureRoomArgs {
  room_id: string
  capture_id: string
  pattern: CapturePattern
}

/** Args shape per intent name, mirroring relay/intent_v1.py _parse_args. */
export interface IntentArgsByName {
  arm: EmptyArgs
  disarm: EmptyArgs
  estop: EmptyArgs
  select: SelectArgs
  takeoff: EmptyArgs
  land: EmptyArgs
  land_all: EmptyArgs
  hold: EmptyArgs
  translate: TranslateArgs
  altitude: DeltaArgs
  formation_next: EmptyArgs
  formation_set: FormationSetArgs
  spacing: DeltaArgs
  come_home: EmptyArgs
  sweep: SweepArgs
  capture_room: CaptureRoomArgs
}

export type IntentArgs = IntentArgsByName[ConsoleIntentName]

export interface IntentV1 {
  v: 1
  t: number
  type: 'intent'
  intent_id: string
  retry_of: string | null
  source: IntentSource
  session: string
  name: ConsoleIntentName
  args: IntentArgs
  selection: DroneId[]
  mode: 'indoor'
  confirm: boolean
}

export type MediaStreamStatus = 'live' | 'offline' | 'unreported'

export interface MediaStreamState {
  status: MediaStreamStatus
  last_frame_at: number | null
}

export interface RelayAircraftState {
  drone_id: DroneId
  connection_epoch: number
  membership: MembershipState
  readiness_reasons: string[]
  flight_state: string | null
  battery: number | null
  link: number | null
  pos_quality: number | null
  control_authority: boolean
  rc_safety_operator_present: boolean
  last_seen_at: number | null
  camera_patterns: string[]
  selectable: boolean
  adapter_id: string
  adapter_capabilities: string[]
  home_pose: unknown
  telemetry: unknown
  membership_history: unknown[]
  membership_history_truncated: number
  video?: MediaStreamState
}

export interface RelayStateEvent {
  v: 1
  t: number
  type: 'state'
  event_id: string
  session: string
  roster_version: number
  state_sequence?: number
  armed: boolean
  estop: boolean
  selection: DroneId[]
  formation: string
  spacing: number
  mode: string
  capability_profile: string
  enabled_intent_names: ConsoleIntentName[]
  pending: Record<string, unknown> | null
  accepted_plan: Record<string, unknown> | null
  drones: RelayAircraftState[]
  invalidated_intent_ids?: string[]
  invalidation_reason?: 'graceful_leave_roster_change'
  prior_roster_version?: number
  cleared_control_fields?: Array<'selection' | 'pending' | 'accepted_plan'>
}

export type MembershipAction =
  | 'join'
  | 'readiness'
  | 'graceful_leave'
  | 'graceful_leave_completed'
  | 'unexpected_loss'
  | 'telemetry_stale'
  | 'telemetry_recovered'

export interface RelayMembershipEvent {
  v: 1
  t: number
  type: 'membership'
  event_id: string
  session: string
  roster_version: number
  action: MembershipAction
  drone_id: DroneId
  connection_epoch: number
  membership: MembershipState
  readiness_reasons: string[]
  adapter_id: string | null
  capabilities: string[]
  provenance:
    | 'adapter_signature'
    | 'relay_transport_attestation'
    | 'relay_freshness_attestation'
    | 'authenticated_adapter_telemetry'
  reason: string | null
}

export type BackendIntentStatus =
  | 'accepted'
  | 'refused'
  | 'executing'
  | 'completed'
  | 'failed'
  | 'invalidated'

export interface RelayAuthAcceptedEvent {
  v: 1
  t: number
  type: 'auth.accepted'
  event_id: string
  session: string
  source: IntentSource | 'adapter'
  drone_id: DroneId | null
}

export interface RelayAuthRefusedEvent {
  v: 1
  t: number
  type: 'auth.refused'
  event_id: string
  session: string
  status: 'refused'
  reason: string
  detail: string
}

export interface RelayAcknowledgementEvent {
  v: 1
  t: number
  type: 'acknowledgement'
  event_id: string
  session: string
  intent_id: string
  status: Exclude<BackendIntentStatus, 'refused'>
  command_id: string | null
  source: string
  reason: string | null
  detail: string | null
  roster_version: number
  drone_id: DroneId | null
  connection_epoch: number | null
}

export interface RelayRefusalEvent {
  v: 1
  t: number
  type: 'refusal'
  event_id: string
  session: string
  intent_id: string | null
  command_id: string | null
  status: 'refused'
  source: string
  reason: string
  detail: string
  roster_version: number
  drone_id: DroneId | null
  connection_epoch: number | null
}

/** Appendix B telemetry is followed by an authoritative state projection. */
export interface RelayTelemetryEvent {
  v: 1
  t: number
  type: 'telemetry'
  event_id: string
  session: string
  drone: DroneId
  connection_epoch: number
  x: number
  y: number
  z: number
  vx: number
  vy: number
  vz: number
  battery: number
  state: string
  link: number
  pos_quality: number
}

export interface RelaySafetyActionEvent {
  v: 1
  t: number
  type: 'safety_action'
  event_id: string
  session: string
  drone_id: DroneId
  connection_epoch: number
  reason: 'link_loss'
  action: 'hold' | 'failsafe'
  loss_behavior: 'hold' | 'failsafe'
}

export type RelayServerEvent =
  | RelayAcknowledgementEvent
  | RelayAuthAcceptedEvent
  | RelayAuthRefusedEvent
  | RelayMembershipEvent
  | RelayRefusalEvent
  | RelayStateEvent
  | RelaySafetyActionEvent
  | RelayTelemetryEvent

export interface RelayAuthFrame {
  v: 1
  type: 'auth'
  source: IntentSource
  token: string
}

/**
 * Mirror of relay/voice.py VoicePlan: the compiler's validated preview carried
 * on a `voice_outcome`. It is never an emitted intent. `plan` carries ordered
 * Intent v1 drafts the console stages one at a time after the operator
 * confirms; `clarify` carries options and emits nothing; `refuse` and
 * `unsupported` carry a typed compiler reason; `cancel_pending` names the
 * pending intent the operator may cancel.
 */
export type VoicePlanKind = 'plan' | 'clarify' | 'unsupported' | 'refuse' | 'cancel_pending'

export const VOICE_PLAN_KINDS: readonly VoicePlanKind[] = [
  'plan',
  'clarify',
  'unsupported',
  'refuse',
  'cancel_pending',
]

export interface VoicePlanStep {
  index: number
  /** Relay-minted deterministic identity bound to this exact audited plan step. */
  intent_id: string
  name: ConsoleIntentName
  args: Record<string, unknown>
  selection: DroneId[]
  mode: 'indoor'
  /** Mirror of the arbiter's confirmation gate for this name. */
  confirm_required: boolean
  /** The compiler's deterministic grounding notes for this step. */
  notes: string[]
}

export interface VoicePlan {
  v: 1
  kind: VoicePlanKind
  transcript: string
  reason: string | null
  detail: string | null
  options: string[]
  steps: VoicePlanStep[]
  compiled_at_ms: number
  /** Set only for kind `plan`; the relay refuses to ground a step past it. */
  expires_at_ms: number | null
  /** The relay state event the plan was grounded on. */
  state_event_id: string
  roster_version: number
  session: string
  correlation_id: string
  plan_digest: string | null
  model: string
  prompt_schema_version: string
  response_source: string
  pending_intent_id: string | null
}

export const MAX_VOICE_PLAN_STEPS = 8
const MAX_VOICE_PLAN_TEXT_CHARS = 500
const VOICE_PLAN_FIELDS = [
  'v',
  'kind',
  'transcript',
  'reason',
  'detail',
  'options',
  'steps',
  'compiled_at_ms',
  'expires_at_ms',
  'state_event_id',
  'roster_version',
  'session',
  'correlation_id',
  'plan_digest',
  'model',
  'prompt_schema_version',
  'response_source',
  'pending_intent_id',
] as const
const VOICE_PLAN_STEP_FIELDS = [
  'index',
  'intent_id',
  'name',
  'args',
  'selection',
  'mode',
  'confirm_required',
  'notes',
] as const

function isBoundedText(value: unknown, limit = MAX_VOICE_PLAN_TEXT_CHARS): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= limit &&
    // Mirrors relay/voice.py: no control characters in operator-visible text.
    // eslint-disable-next-line no-control-regex
    !/[\u0000-\u001f]/.test(value)
  )
}

function isNullableBoundedText(value: unknown): value is string | null {
  return value === null || isBoundedText(value)
}

function hasExactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  const keys = Object.keys(value)
  return keys.length === fields.length && fields.every((field) => field in value)
}

function sameIds(left: DroneId[], right: DroneId[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index])
}

function isJsonNative(value: unknown, depth = 0): boolean {
  if (depth > 8) return false
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (Array.isArray(value)) return value.every((item) => isJsonNative(item, depth + 1))
  if (isRecord(value)) return Object.values(value).every((item) => isJsonNative(item, depth + 1))
  return false
}

function isVoicePlanStep(value: unknown, index: number): value is VoicePlanStep {
  if (!isRecord(value) || !hasExactFields(value, VOICE_PLAN_STEP_FIELDS)) return false
  const structurallyValid =
    value.index === index &&
    typeof value.intent_id === 'string' &&
    /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.intent_id) &&
    typeof value.name === 'string' &&
    (CONSOLE_INTENT_NAMES as readonly string[]).includes(value.name) &&
    isRecord(value.args) &&
    isJsonNative(value.args) &&
    isDroneIds(value.selection) &&
    new Set(value.selection as DroneId[]).size === (value.selection as DroneId[]).length &&
    value.mode === 'indoor' &&
    typeof value.confirm_required === 'boolean' &&
    Array.isArray(value.notes) &&
    value.notes.length <= 8 &&
    value.notes.every((note) => isBoundedText(note))
  if (!structurallyValid) return false
  const candidate: unknown = {
    v: 1,
    t: 0,
    type: 'intent',
    intent_id: value.intent_id,
    retry_of: null,
    source: 'language',
    session: 'voice-plan-validation',
    name: value.name,
    args: value.args,
    selection: value.selection,
    mode: value.mode,
    confirm: true,
  }
  if (!isConsoleIntentV1(candidate)) return false
  if (value.confirm_required !== requiresConfirmation(candidate.name)) return false
  return (
    candidate.name !== 'select' ||
    ('ids' in candidate.args && sameIds(candidate.selection, candidate.args.ids))
  )
}

/** Structural validator for the relay's VoicePlan; mirrors relay/voice.py parse_voice_plan. */
export function isVoicePlan(value: unknown): value is VoicePlan {
  if (!isRecord(value) || !hasExactFields(value, VOICE_PLAN_FIELDS)) return false
  if (value.v !== 1 || !(VOICE_PLAN_KINDS as readonly unknown[]).includes(value.kind)) return false
  if (!isBoundedText(value.transcript, 4_000) || value.transcript.trim().length === 0) return false
  if (
    !isNullableBoundedText(value.reason) ||
    !isNullableBoundedText(value.detail) ||
    !isNullableBoundedText(value.plan_digest) ||
    !isNullableBoundedText(value.pending_intent_id)
  ) {
    return false
  }
  if (
    !isBoundedText(value.state_event_id, 512) ||
    !isBoundedText(value.session, 512) ||
    !isBoundedText(value.correlation_id, 512) ||
    !isBoundedText(value.model, 512) ||
    !isBoundedText(value.prompt_schema_version, 512) ||
    !isBoundedText(value.response_source, 512)
  ) {
    return false
  }
  if (!isNonNegativeInteger(value.compiled_at_ms) || !isNonNegativeInteger(value.roster_version)) {
    return false
  }
  if (
    value.expires_at_ms !== null &&
    (!isNonNegativeInteger(value.expires_at_ms) || value.expires_at_ms <= value.compiled_at_ms)
  ) {
    return false
  }
  if (
    !Array.isArray(value.options) ||
    value.options.length > 16 ||
    !value.options.every((option) => isBoundedText(option)) ||
    new Set(value.options).size !== value.options.length
  ) {
    return false
  }
  if (
    !Array.isArray(value.steps) ||
    value.steps.length > MAX_VOICE_PLAN_STEPS ||
    !value.steps.every((step, index) => isVoicePlanStep(step, index))
  ) {
    return false
  }
  if (value.kind === 'plan') {
    return (
      value.steps.length > 0 &&
      value.expires_at_ms !== null &&
      typeof value.plan_digest === 'string' &&
      /^[0-9a-f]{64}$/.test(value.plan_digest) &&
      value.reason === null &&
      value.options.length === 0 &&
      value.pending_intent_id === null
    )
  }
  if (value.steps.length > 0 || value.plan_digest !== null || value.expires_at_ms !== null) return false
  if (value.kind === 'cancel_pending') {
    return value.pending_intent_id !== null && value.reason === null && value.options.length === 0
  }
  return value.reason !== null && value.pending_intent_id === null
}

/** Build the only Intent v1 draft permitted from a relay-bound voice step. */
export function intentFromVoicePlanStep(
  plan: VoicePlan,
  step: VoicePlanStep,
  timestamp: number,
): IntentV1 | null {
  if (
    plan.kind !== 'plan' ||
    plan.plan_digest === null ||
    plan.steps[step.index] !== step ||
    !isNonNegativeInteger(timestamp)
  ) {
    return null
  }
  const candidate: unknown = {
    v: 1,
    t: timestamp,
    type: 'intent',
    intent_id: step.intent_id,
    retry_of: null,
    source: 'language',
    session: plan.session,
    name: step.name,
    args: step.args,
    selection: step.selection,
    mode: step.mode,
    // Every language emission is operator-confirmed, even when the arbiter's
    // base policy would permit the same name from a console button immediately.
    confirm: true,
  }
  if (!isConsoleIntentV1(candidate)) return null
  return {
    ...candidate,
    args: structuredClone(candidate.args),
    selection: [...candidate.selection],
    confirm: false,
  }
}

const MEMBERSHIP_STATES = new Set<MembershipState>([
  'registered',
  'ready',
  'leaving',
  'disconnected',
  'degraded',
])
const CAPTURE_PATTERNS = new Set<CapturePattern>(['pano_360', 'reconstruct_8'])
/** Mirror of relay REGISTERED_SOURCES: operator sources bound to their own connection. */
const INTENT_SOURCES = new Set<IntentSource>(['console', 'keyboard', 'webcam', 'language'])
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0
}

function isDroneId(value: unknown): value is DroneId {
  return Number.isInteger(value) && Number(value) > 0
}

function isDroneIds(value: unknown): value is DroneId[] {
  return Array.isArray(value) && value.every(isDroneId) && new Set(value).size === value.length
}

function isIntentDroneIds(value: unknown): value is DroneId[] {
  return (
    Array.isArray(value) &&
    value.length <= MAX_INTENT_DRONE_IDS &&
    value.every(
      (item) => Number.isInteger(item) && Number(item) > 0 && Number(item) <= MAX_INTENT_DRONE_ID,
    ) &&
    new Set(value).size === value.length
  )
}

/** Mirrors trimmed Python `str.isprintable()` and counts Unicode code points. */
function isCanonicalIntentText(value: unknown, maximumCodePoints: number): value is string {
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    value !== value.trim() ||
    Array.from(value).length > maximumCodePoints
  ) {
    return false
  }
  return Array.from(value).every((character) => character === ' ' || !/[\p{C}\p{Z}]/u.test(character))
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isNullableNonEmptyString(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isCapabilityAdvertisement(profile: unknown, enabled: unknown): enabled is ConsoleIntentName[] {
  if (
    typeof profile !== 'string' ||
    !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(profile) ||
    !isStringArray(enabled) ||
    enabled.length === 0 ||
    new Set(enabled).size !== enabled.length ||
    !enabled.every((name) => SUPPORTED_INTENTS.has(name as ConsoleIntentName))
  ) {
    return false
  }
  if (profile !== 'c1_basic_control') return true
  return (
    enabled.length === C1_BASIC_CONTROL_INTENTS.length &&
    C1_BASIC_CONTROL_INTENTS.every((name) => enabled.includes(name))
  )
}

function isNullableDroneId(value: unknown): value is DroneId | null {
  return value === null || isDroneId(value)
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value)
}

function isNullableUnitNumber(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1)
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isNullableRecord(value: unknown): value is Record<string, unknown> | null {
  return value === null || isRecord(value)
}

export function isRelayAircraftState(value: unknown): value is RelayAircraftState {
  if (!isRecord(value)) return false
  const patterns = value.camera_patterns
  const readinessReasons = value.readiness_reasons

  return (
    isDroneId(value.drone_id) &&
    isNonNegativeInteger(value.connection_epoch) &&
    MEMBERSHIP_STATES.has(value.membership as MembershipState) &&
    isStringArray(readinessReasons) &&
    (value.flight_state === null || typeof value.flight_state === 'string') &&
    isNullableUnitNumber(value.battery) &&
    isNullableUnitNumber(value.link) &&
    isNullableUnitNumber(value.pos_quality) &&
    typeof value.control_authority === 'boolean' &&
    typeof value.rc_safety_operator_present === 'boolean' &&
    (value.last_seen_at === null || isNonNegativeInteger(value.last_seen_at)) &&
    isStringArray(patterns) &&
    typeof value.selectable === 'boolean' &&
    typeof value.adapter_id === 'string' &&
    value.adapter_id.length > 0 &&
    isStringArray(value.adapter_capabilities) &&
    'home_pose' in value &&
    'telemetry' in value &&
    Array.isArray(value.membership_history) &&
    isNonNegativeInteger(value.membership_history_truncated) &&
    isVideoStreamState(value.video)
  )
}

function isVideoStreamState(value: unknown): value is MediaStreamState | undefined {
  if (value === undefined) return true
  if (!isRecord(value)) return false
  return (
    Object.keys(value).length === 2 &&
    Object.hasOwn(value, 'status') &&
    Object.hasOwn(value, 'last_frame_at') &&
    (value.status === 'live' || value.status === 'offline' || value.status === 'unreported') &&
    (value.last_frame_at === null || isNonNegativeInteger(value.last_frame_at))
  )
}

function hasBaseEvent(value: Record<string, unknown>): boolean {
  return (
    value.v === 1 &&
    isNonNegativeInteger(value.t) &&
    typeof value.event_id === 'string' &&
    value.event_id.length > 0 &&
    typeof value.session === 'string' &&
    value.session.length > 0
  )
}

/** Parses the M1.1 event seam; unknown frames fail closed. */
export function parseRelayServerEvent(value: unknown): RelayServerEvent | null {
  if (!isRecord(value) || !hasBaseEvent(value) || typeof value.type !== 'string') return null

  if (value.type === 'state') {
    if (
      !isNonNegativeInteger(value.roster_version) ||
      (value.state_sequence !== undefined &&
        (!Number.isSafeInteger(value.state_sequence) || Number(value.state_sequence) < 1)) ||
      typeof value.armed !== 'boolean' ||
      typeof value.estop !== 'boolean' ||
      !isDroneIds(value.selection) ||
      typeof value.formation !== 'string' ||
      !isFiniteNumber(value.spacing) ||
      typeof value.mode !== 'string' ||
      !isCapabilityAdvertisement(value.capability_profile, value.enabled_intent_names) ||
      !isNullableRecord(value.pending) ||
      !isNullableRecord(value.accepted_plan) ||
      !Array.isArray(value.drones) ||
      !value.drones.every(isRelayAircraftState) ||
      (value.invalidated_intent_ids !== undefined && !isStringArray(value.invalidated_intent_ids)) ||
      (value.invalidation_reason !== undefined &&
        value.invalidation_reason !== 'graceful_leave_roster_change') ||
      (value.prior_roster_version !== undefined &&
        !isNonNegativeInteger(value.prior_roster_version)) ||
      (value.cleared_control_fields !== undefined &&
        (!Array.isArray(value.cleared_control_fields) ||
          !value.cleared_control_fields.every((field) =>
            ['selection', 'pending', 'accepted_plan'].includes(String(field)),
          )))
    ) {
      return null
    }
    return value as unknown as RelayStateEvent
  }

  if (value.type === 'membership') {
    if (
      !isNonNegativeInteger(value.roster_version) ||
      ![
        'join',
        'readiness',
        'graceful_leave',
        'graceful_leave_completed',
        'unexpected_loss',
        'telemetry_stale',
        'telemetry_recovered',
      ].includes(String(value.action)) ||
      !isDroneId(value.drone_id) ||
      !isNonNegativeInteger(value.connection_epoch) ||
      !MEMBERSHIP_STATES.has(value.membership as MembershipState) ||
      !isStringArray(value.readiness_reasons) ||
      !(value.adapter_id === null || typeof value.adapter_id === 'string') ||
      !isStringArray(value.capabilities) ||
      ![
        'adapter_signature',
        'relay_transport_attestation',
        'relay_freshness_attestation',
        'authenticated_adapter_telemetry',
      ].includes(String(value.provenance)) ||
      !isNullableString(value.reason)
    ) {
      return null
    }
    return value as unknown as RelayMembershipEvent
  }

  if (value.type === 'auth.accepted') {
    if (
      !(INTENT_SOURCES.has(value.source as IntentSource) || value.source === 'adapter') ||
      !isNullableDroneId(value.drone_id)
    ) {
      return null
    }
    return value as unknown as RelayAuthAcceptedEvent
  }

  if (value.type === 'auth.refused') {
    if (
      value.status !== 'refused' ||
      typeof value.reason !== 'string' ||
      typeof value.detail !== 'string'
    ) {
      return null
    }
    return value as unknown as RelayAuthRefusedEvent
  }

  if (value.type === 'telemetry') {
    if (
      !isDroneId(value.drone) ||
      !isNonNegativeInteger(value.connection_epoch) ||
      !['x', 'y', 'z', 'vx', 'vy', 'vz'].every((field) => isFiniteNumber(value[field])) ||
      !isFiniteNumber(value.battery) ||
      value.battery < 0 ||
      value.battery > 1 ||
      typeof value.state !== 'string' ||
      value.state.length === 0 ||
      !isFiniteNumber(value.link) ||
      value.link < 0 ||
      value.link > 1 ||
      !isFiniteNumber(value.pos_quality) ||
      value.pos_quality < 0 ||
      value.pos_quality > 1
    ) {
      return null
    }
    return value as unknown as RelayTelemetryEvent
  }

  if (value.type === 'safety_action') {
    if (
      !isDroneId(value.drone_id) ||
      !isNonNegativeInteger(value.connection_epoch) ||
      value.reason !== 'link_loss' ||
      !['hold', 'failsafe'].includes(String(value.action)) ||
      !['hold', 'failsafe'].includes(String(value.loss_behavior))
    ) {
      return null
    }
    return value as unknown as RelaySafetyActionEvent
  }

  if (value.type === 'acknowledgement') {
    if (
      typeof value.intent_id !== 'string' ||
      value.intent_id.length === 0 ||
      !['accepted', 'executing', 'completed', 'failed', 'invalidated'].includes(
        String(value.status),
      ) ||
      !isNullableNonEmptyString(value.command_id) ||
      typeof value.source !== 'string' ||
      !isNullableString(value.reason) ||
      !isNullableString(value.detail) ||
      !isNonNegativeInteger(value.roster_version) ||
      !isNullableDroneId(value.drone_id) ||
      !isNullableNonNegativeInteger(value.connection_epoch)
    ) {
      return null
    }
    return value as unknown as RelayAcknowledgementEvent
  }

  if (value.type === 'refusal') {
    if (
      !(
        value.intent_id === null ||
        (typeof value.intent_id === 'string' && value.intent_id.length > 0)
      ) ||
      typeof value.reason !== 'string' ||
      typeof value.detail !== 'string' ||
      !isNullableNonEmptyString(value.command_id) ||
      value.status !== 'refused' ||
      typeof value.source !== 'string' ||
      !isNonNegativeInteger(value.roster_version) ||
      !isNullableDroneId(value.drone_id) ||
      !isNullableNonNegativeInteger(value.connection_epoch)
    ) {
      return null
    }
    return value as unknown as RelayRefusalEvent
  }

  return null
}

/** Local conformance check for the console-produced subset of Intent v1. */
export function isConsoleIntentV1(value: unknown): value is IntentV1 {
  if (!isRecord(value)) return false
  const keys = Object.keys(value)
  const allowedKeys = new Set([
    'v',
    't',
    'type',
    'intent_id',
    'retry_of',
    'source',
    'session',
    'name',
    'args',
    'selection',
    'mode',
    'confirm',
  ])
  if (keys.some((key) => !allowedKeys.has(key)) || keys.some((key) => value[key] === undefined)) {
    return false
  }
  if (
    value.v !== 1 ||
    !Number.isSafeInteger(value.t) ||
    Number(value.t) < 0 ||
    value.type !== 'intent' ||
    !isCanonicalIntentText(value.intent_id, MAX_INTENT_IDENTIFIER_CODE_POINTS) ||
    !(
      value.retry_of === null ||
      (isCanonicalIntentText(value.retry_of, MAX_INTENT_IDENTIFIER_CODE_POINTS) &&
        value.retry_of !== value.intent_id)
    ) ||
    !isCanonicalIntentText(value.source, MAX_INTENT_SOURCE_CODE_POINTS) ||
    !INTENT_SOURCES.has(value.source as IntentSource) ||
    !isCanonicalIntentText(value.session, MAX_INTENT_SESSION_CODE_POINTS) ||
    !isCanonicalIntentText(value.name, MAX_INTENT_NAME_CODE_POINTS) ||
    !(CONSOLE_INTENT_NAMES as readonly string[]).includes(String(value.name)) ||
    !isRecord(value.args) ||
    !isIntentDroneIds(value.selection) ||
    value.mode !== 'indoor' ||
    typeof value.confirm !== 'boolean'
  ) {
    return false
  }
  const name = value.name as ConsoleIntentName
  const selection = value.selection as DroneId[]
  if (!hasValidArgs(name, value.args)) return false
  if (requiresConfirmation(name) && !value.confirm) return false
  return hasValidSelection(name, selection)
}

const FORMATION_NAMES = new Set<FormationName>(['line', 'column', 'circle', 'grid', 'V'])

/** Mirrors relay/intent_v1.py _parse_args for the console-built subset. */
function hasValidArgs(name: ConsoleIntentName, args: Record<string, unknown>): boolean {
  const keys = Object.keys(args)
  switch (name) {
    case 'select':
      return keys.length === 1 && isIntentDroneIds(args.ids) && args.ids.length > 0
    case 'translate':
      return keys.length === 2 && isFiniteNumber(args.dx) && isFiniteNumber(args.dy)
    case 'altitude':
    case 'spacing':
      return keys.length === 1 && isFiniteNumber(args.delta)
    case 'formation_set':
      return keys.length === 1 && FORMATION_NAMES.has(args.name as FormationName)
    case 'sweep':
      return keys.length === 0 || (keys.length === 1 && isSweepBox(args.box))
    case 'capture_room':
      return (
        keys.length === 3 &&
        isCanonicalIntentText(args.room_id, MAX_INTENT_IDENTIFIER_CODE_POINTS) &&
        isCanonicalIntentText(args.capture_id, MAX_INTENT_IDENTIFIER_CODE_POINTS) &&
        CAPTURE_PATTERNS.has(args.pattern as CapturePattern)
      )
    case 'arm':
    case 'disarm':
    case 'estop':
    case 'takeoff':
    case 'land':
    case 'land_all':
    case 'hold':
    case 'formation_next':
    case 'come_home':
      return keys.length === 0
  }
}

function isSweepBox(value: unknown): value is SweepBox {
  if (!isRecord(value)) return false
  const keys = Object.keys(value)
  if (
    keys.length !== 4 ||
    !['min_x', 'max_x', 'min_y', 'max_y'].every((key) => keys.includes(key)) ||
    !isFiniteNumber(value.min_x) ||
    !isFiniteNumber(value.max_x) ||
    !isFiniteNumber(value.min_y) ||
    !isFiniteNumber(value.max_y)
  ) {
    return false
  }
  return value.min_x < value.max_x && value.min_y < value.max_y
}

/** The brief's selection rules; capture_room's is also the relay's own scope check. */
function hasValidSelection(name: ConsoleIntentName, selection: DroneId[]): boolean {
  switch (SELECTION_RULES[name]) {
    case 'any':
    case 'all':
      return true
    case 'fleet':
      return selection.length === 0
    case 'at least one':
    case 'selected':
      return selection.length > 0
    case 'exactly one':
      return selection.length === 1
  }
}
