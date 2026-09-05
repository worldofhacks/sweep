/**
 * Console-side mirror of the frozen Intent v1 contract in relay/intent_v1.py.
 *
 * Relay event envelopes are deliberately kept in this one module while M1.1 is
 * integrated. Components and reducers consume these normalized shapes and do
 * not infer transport, planner, or safety semantics.
 */

export type DroneId = number
export type CapturePattern = 'pano_360' | 'reconstruct_8'
export type IntentSource = 'console' | 'keyboard'
export type FormationName = 'line' | 'column' | 'circle' | 'grid' | 'V'

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
 * Mirror of relay/intent_v1.py M20_SUPPORTED_NAMES. The relay refuses every
 * other name with reason `unsupported`; the console still sends them so the
 * refusal is recorded rather than hidden.
 */
export const M20_SUPPORTED_INTENTS: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'arm',
  'select',
  'takeoff',
  'translate',
  'hold',
  'come_home',
  'land_all',
  'estop',
  'capture_room',
])

export function isSupportedAtM20(name: ConsoleIntentName): boolean {
  return M20_SUPPORTED_INTENTS.has(name)
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
export type SweepArgs = EmptyArgs | { box: Record<string, unknown> }
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
  video?: MediaStreamState
}

export interface RelayStateEvent {
  v: 1
  t: number
  type: 'state'
  event_id: string
  session: string
  roster_version: number
  armed: boolean
  estop: boolean
  selection: DroneId[]
  formation: string
  spacing: number
  mode: string
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

export type RelayServerEvent =
  | RelayAcknowledgementEvent
  | RelayAuthAcceptedEvent
  | RelayAuthRefusedEvent
  | RelayMembershipEvent
  | RelayRefusalEvent
  | RelayStateEvent
  | RelayTelemetryEvent

export interface RelayAuthFrame {
  v: 1
  type: 'auth'
  source: IntentSource
  token: string
}

const MEMBERSHIP_STATES = new Set<MembershipState>([
  'registered',
  'ready',
  'leaving',
  'disconnected',
  'degraded',
])
const CAPTURE_PATTERNS = new Set<CapturePattern>(['pano_360', 'reconstruct_8'])
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

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isNullableNonEmptyString(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
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
      typeof value.armed !== 'boolean' ||
      typeof value.estop !== 'boolean' ||
      !isDroneIds(value.selection) ||
      typeof value.formation !== 'string' ||
      !isFiniteNumber(value.spacing) ||
      typeof value.mode !== 'string' ||
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
      !['console', 'keyboard', 'adapter'].includes(String(value.source)) ||
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
    !isNonNegativeInteger(value.t) ||
    value.type !== 'intent' ||
    typeof value.intent_id !== 'string' ||
    value.intent_id.length === 0 ||
    !(
      value.retry_of === null ||
      (typeof value.retry_of === 'string' &&
        value.retry_of.length > 0 &&
        value.retry_of !== value.intent_id)
    ) ||
    (value.source !== 'console' && value.source !== 'keyboard') ||
    typeof value.session !== 'string' ||
    value.session.length === 0 ||
    !(CONSOLE_INTENT_NAMES as readonly string[]).includes(String(value.name)) ||
    !isRecord(value.args) ||
    !isDroneIds(value.selection) ||
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
      return keys.length === 1 && isDroneIds(args.ids) && args.ids.length > 0
    case 'translate':
      return keys.length === 2 && isFiniteNumber(args.dx) && isFiniteNumber(args.dy)
    case 'altitude':
    case 'spacing':
      return keys.length === 1 && isFiniteNumber(args.delta)
    case 'formation_set':
      return keys.length === 1 && FORMATION_NAMES.has(args.name as FormationName)
    case 'sweep':
      return keys.length === 0 || (keys.length === 1 && isRecord(args.box))
    case 'capture_room':
      return (
        keys.length === 3 &&
        typeof args.room_id === 'string' &&
        args.room_id.length > 0 &&
        typeof args.capture_id === 'string' &&
        args.capture_id.length > 0 &&
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
