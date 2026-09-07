/**
 * Console-side mirror of the frozen Intent v1 contract in relay/intent_v1.py.
 *
 * Relay event envelopes are deliberately kept in this one module while M1.1 is
 * integrated. Components and reducers consume these normalized shapes and do
 * not infer transport, planner, or safety semantics.
 */

export type DroneId = number
import { parseObservation, type Observation } from './observation'

export type NodeType = 'aircraft' | 'ground'
/** Internal presentation class derived from the relay's canonical node type. */
export type DeviceClass = 'aircraft' | 'ground_vehicle'
export const DEVICE_CLASSES: readonly DeviceClass[] = ['aircraft', 'ground_vehicle']
export type CapturePattern = 'pano_360' | 'reconstruct_8'
export type IntentSource = 'console' | 'keyboard' | 'webcam' | 'language'
export const FORMATION_NAMES = ['line', 'column', 'wedge', 'diamond'] as const
export type FormationName = (typeof FORMATION_NAMES)[number]

// Intent producer ceilings mirrored from relay/intent_v1.py. JavaScript numbers
// use their exact integer ceiling; the relay additionally accepts signed-Long
// timestamps from non-JavaScript peers.
export const MAX_INTENT_IDENTIFIER_CODE_POINTS = 128
export const MAX_INTENT_SESSION_CODE_POINTS = 512
export const MAX_INTENT_SOURCE_CODE_POINTS = 64
export const MAX_INTENT_NAME_CODE_POINTS = 64
/** Shared transport bound with relay.fleet_limits; not a flight qualification limit. */
export const MAX_FLEET_DEVICES = 64
export const MAX_INTENT_DRONE_IDS = MAX_FLEET_DEVICES
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
  | 'body_pulse'
  | 'robot_peripheral'
  | 'camera_control'
  | 'altitude'
  | 'formation_next'
  | 'formation_set'
  | 'spacing'
  | 'come_home'
  | 'sweep'
  | 'capture_room'
  | 'ground_velocity'
  | 'survey_area'

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
  'body_pulse',
  'robot_peripheral',
  'camera_control',
  'altitude',
  'formation_next',
  'formation_set',
  'spacing',
  'come_home',
  'sweep',
  'capture_room',
  'ground_velocity',
  'survey_area',
]

/** The exact profile emitted by a C1 relay. */
export const C1_BASIC_CONTROL_INTENTS: readonly ConsoleIntentName[] = [
  'arm',
  'altitude',
  'capture_room',
  'come_home',
  'estop',
  'hold',
  'land',
  'land_all',
  'select',
  'takeoff',
  'translate',
]

export const SUPERVISED_VERTICAL_INTENTS: readonly ConsoleIntentName[] = [
  'arm',
  'estop',
  'hold',
  'land',
  'land_all',
  'select',
  'takeoff',
  'ground_velocity',
  'survey_area',
]

/** The exact profile emitted by a C2 simulator relay. */
export const C2_FLEET_OPERATIONS_INTENTS: readonly ConsoleIntentName[] = [
  'arm',
  'altitude',
  'capture_room',
  'come_home',
  'disarm',
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

/** Every intent implemented by this console, independently of deployment release. */
export const SUPPORTED_INTENTS: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>(
  [...C2_FLEET_OPERATIONS_INTENTS, 'ground_velocity', 'survey_area'],
)

export function isSupportedIntent(name: ConsoleIntentName): boolean {
  return SUPPORTED_INTENTS.has(name)
}

/**
 * Console policy from the design brief: these intents never leave the console
 * without the operator confirming the exact envelope. The relay additionally
 * requires every webcam flight action, including session enable, to be confirmed.
 */
export const CONFIRM_REQUIRED_INTENTS: ReadonlySet<ConsoleIntentName> = new Set<ConsoleIntentName>([
  'robot_peripheral',
  'camera_control',
  'body_pulse',
  'takeoff',
  'land',
  'land_all',
  'sweep',
  'capture_room',
  'ground_velocity',
  'survey_area',
])

export function requiresConfirmation(name: ConsoleIntentName): boolean {
  return CONFIRM_REQUIRED_INTENTS.has(name)
}

/** Selection rule per intent, from the brief's controls table. */
export type SelectionRule = 'any' | 'at least one' | 'selected' | 'all' | 'exactly one' | 'fleet' | 'connected device'

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
  body_pulse: 'selected',
  robot_peripheral: 'connected device',
  camera_control: 'connected device',
  altitude: 'selected',
  formation_next: 'selected',
  formation_set: 'selected',
  spacing: 'selected',
  come_home: 'selected',
  sweep: 'selected',
  capture_room: 'exactly one',
  ground_velocity: 'exactly one',
  survey_area: 'exactly one',
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
export type RobotPeripheralArgs =
  | { kind: 'neck'; position: number }
  | { kind: 'speech' | 'screen'; text: string }
  | { kind: 'lights'; h: number; s: number; v: number }

export type CameraControlArgs = { kind: 'ready' | 'photo' } | { kind: 'gimbal'; pitch_mdeg: number }

export function isCameraControlArgs(value: unknown): value is CameraControlArgs {
  if (!isRecord(value)) return false
  if (value.kind === 'ready' || value.kind === 'photo') return Object.keys(value).length === 1
  return value.kind === 'gimbal' && Object.keys(value).length === 2 &&
    Number.isSafeInteger(value.pitch_mdeg) && Math.abs(Number(value.pitch_mdeg)) <= 180_000
}

export function isRobotPeripheralArgs(value: unknown): value is RobotPeripheralArgs {
  if (!isRecord(value)) return false
  const keys = Object.keys(value)
  if (value.kind === 'neck') return keys.length === 2 && keys.includes('position') && Number.isInteger(value.position) && Number(value.position) >= 300 && Number(value.position) <= 650
  if (value.kind === 'lights') return keys.length === 4 && ['h', 's', 'v'].every((key) => Number.isInteger(value[key]) && Number(value[key]) >= 0 && Number(value[key]) <= 255)
  if (value.kind === 'speech' || value.kind === 'screen') return keys.length === 2 && typeof value.text === 'string' &&
    (value.text.length > 0 || value.kind === 'screen') && Array.from(value.text).length <= 240 && value.text.trim() === value.text && !/[\p{C}\p{Z}]/u.test(value.text.replaceAll(' ', ''))
  return false
}

export interface BodyPulseArgs {
  /** Signed speed in the aircraft body frame: positive forward, negative backward. */
  forward_mm_s: number
  /** Adapter-enforced duration, 100–500 ms. */
  duration_ms: number
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
export interface GroundVelocityArgs {
  linear_mm_s: number
  angular_mrad_s: number
  duration_ms: number
}
export interface SurveyAreaArgs {
  area_id: string
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
  body_pulse: BodyPulseArgs
  robot_peripheral: RobotPeripheralArgs
  camera_control: CameraControlArgs
  altitude: DeltaArgs
  formation_next: EmptyArgs
  formation_set: FormationSetArgs
  spacing: DeltaArgs
  come_home: EmptyArgs
  sweep: SweepArgs
  capture_room: CaptureRoomArgs
  ground_velocity: GroundVelocityArgs
  survey_area: SurveyAreaArgs
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

export interface DeviceCameraState extends MediaStreamState {
  camera_id: string
  label: string
  stream: string
}

export function validMediaStreamName(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(value)
}

export type SensorKind = 'lidar_scan'

/** The relay's per-device sensor projection, mirroring `video`. */
export interface SensorState {
  kind: SensorKind | null
  last_scan_at: number | null
}

export interface RelayAircraftState {
  /** Browser-derived freshness, discarded from incoming wire data. */
  client_observation?: import('../control/observation').DeviceObservation
  drone_id: DroneId
  /** Canonical relay identity. The display class is derived locally from this field. */
  node_type?: NodeType
  /** Absent on the wire from a relay without device classes; parsed as aircraft. */
  device_class: DeviceClass
  /** 1-based ordinal within the device's class; absent on the wire means the drone id. */
  unit: number
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
  home_pose: { x: number; y: number; z: number } | null
  telemetry: RelayTelemetryState | null
  node_status?: RelayNodeStatusState | null
  camera_capabilities?: RelayCameraCapabilitiesState | null
  /** Current-epoch public advisory; never grants control. */
  capture_readiness?: RelayCaptureReadinessEvent | null
  membership_history: unknown[]
  membership_history_truncated: number
  video?: MediaStreamState
  /** Explicit per-device camera wiring; absent preserves the legacy primary stream. */
  cameras?: DeviceCameraState[]
  sensor?: SensorState
  ground_readiness?: { source_id: string | null } | null
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
  formation: 'none' | FormationName
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
  node_type?: NodeType
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

/** Public, signature-free node events normalized by relay/contracts.py. */
interface RelayNodeEventEnvelope {
  v: 1
  t: number
  event_id: string
  session: string
  drone_id: DroneId
  connection_epoch: number
}

export interface RelayCapabilitiesEvent extends RelayNodeEventEnvelope {
  type: 'capabilities'
  native_panorama_modes: string[]
  photo_capture: boolean
  gimbal_pitch_min_deg: number
  gimbal_pitch_max_deg: number
  horizontal_fov_deg: number
  storage_remaining_bytes: number
  media_retrieval: boolean
  aircraft_model: string
  aircraft_firmware: string
  rc_firmware: string
  phone_model: string
  android_version: string
  sdk_version: string
  measured_hfov_deg: number | null
}

export type DeviceTelemetryValue = null | boolean | number | string | DeviceTelemetryValue[] | { [key: string]: DeviceTelemetryValue }
export type DeviceTelemetry = { [key: string]: DeviceTelemetryValue }
export const MAX_DEVICE_TELEMETRY_BYTES = 16 * 1024
export const MAX_DEVICE_TELEMETRY_DEPTH = 4
/** Older relay fixtures can report only a subset; absent values remain unreported. */
export type RelayTelemetryState = Partial<Omit<RelayTelemetryEvent, 'event_id' | 'session' | 'connection_epoch'>> & {
  yaw_deg?: number
  heading_deg?: number
  fresh?: boolean
}
export type RelayNodeStatusState = Omit<RelayNodeStatusEvent, 'event_id' | 'session' | 'connection_epoch' | 'local_height'> & {
  local_height?: RelayLocalHeight | RelayProjectedLocalHeight | null
}
export type RelayCameraCapabilitiesState = Omit<RelayCapabilitiesEvent, 'event_id' | 'session' | 'connection_epoch'>

export interface RelayLocalHeight {
  z_m: number
  source: 'flight_controller_altitude'
  age_ms: number
}

export interface RelayProjectedLocalHeight extends RelayLocalHeight {
  reported_at_ms: number
}

export interface RelayNodeStatusEvent extends RelayNodeEventEnvelope {
  type: 'node_status'
  virtual_stick_enabled: boolean
  control_authority: boolean
  authority_change_reason: string | null
  watchdog_state: 'nominal' | 'hold' | 'failsafe'
  video_publish_state: 'stopped' | 'connecting' | 'publishing' | 'failed'
  phone_battery_percent: number
  phone_thermal_state: 'none' | 'light' | 'moderate' | 'severe' | 'critical' | 'emergency' | 'shutdown'
  local_height?: RelayLocalHeight
  device_telemetry?: DeviceTelemetry
}

export interface RelayCaptureReadinessEvent extends RelayNodeEventEnvelope {
  type: 'capture_readiness'
  room_id: string | null
  capture_id: string | null
  guidance_mode: 'visual_advisory' | 'registered_metric'
  pose_source: string
  pose_ok: boolean
  clearance_ok: boolean
  camera_ok: boolean
  storage_ok: boolean
  motion_ok: boolean
  image_quality_ok: boolean
  coverage_missing: number[]
  next_heading_deg: number | null
  suggested_delta: { kind: 'yaw' | 'gimbal'; degrees: number } | null
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

/** The device's pose at scan time, in the same frame as its telemetry. */
export interface SensorPose {
  x: number
  y: number
  /** Counter-clockwise from +x, in [0, 360). */
  yaw_deg: number
}

export type SensorAngleIncrement = 0.5 | 1 | 2
export const SENSOR_ANGLE_INCREMENTS: readonly SensorAngleIncrement[] = [0.5, 1, 2]
export const MAX_SENSOR_RANGES = 720
export const MAX_SENSOR_RANGE_CM = 65_535
export const MAX_SENSOR_FRAME_BYTES = 8_192

/**
 * A node's lidar scan, fanned out by the relay as received. Angle 0 points
 * along the device's forward axis and angles increase counter-clockwise;
 * a range of 0 means no return.
 */
export interface RelaySensorEvent {
  v: 1
  t: number
  type: 'sensor'
  event_id: string
  session: string
  drone_id: DroneId
  connection_epoch: number
  kind: SensorKind
  pose: SensorPose
  angle_min_deg: number
  angle_increment_deg: SensorAngleIncrement
  range_min_m: number
  range_max_m: number
  ranges_cm: number[]
}

export type RelayServerEvent =
  | Observation
  | RelayAcknowledgementEvent
  | RelayAuthAcceptedEvent
  | RelayAuthRefusedEvent
  | RelayMembershipEvent
  | RelayRefusalEvent
  | RelayStateEvent
  | RelaySafetyActionEvent
  | RelaySensorEvent
  | RelayTelemetryEvent
  | RelayCapabilitiesEvent
  | RelayNodeStatusEvent
  | RelayCaptureReadinessEvent

/**
 * Metadata the relay's map endpoint returns in headers beside its PNG raster.
 * Row 0 of the image is the top (maximum y); the origin is the world position
 * of the bottom-left cell corner.
 */
export interface MapMetadata {
  resolution_m: number
  origin_x: number
  origin_y: number
  width: number
  height: number
  updated_at: number
}

export const MAP_METADATA_HEADERS: Readonly<Record<keyof MapMetadata, string>> = {
  resolution_m: 'X-Sweep-Map-Resolution-M',
  origin_x: 'X-Sweep-Map-Origin-X',
  origin_y: 'X-Sweep-Map-Origin-Y',
  width: 'X-Sweep-Map-Width',
  height: 'X-Sweep-Map-Height',
  updated_at: 'X-Sweep-Map-Updated-At',
}

/** Reads the map headers; any missing or malformed value fails closed. */
export function parseMapMetadata(read: (name: string) => string | null): MapMetadata | null {
  const number = (name: string): number | null => {
    const raw = read(name)
    if (raw === null || raw.trim().length === 0) return null
    const value = Number(raw)
    return Number.isFinite(value) ? value : null
  }
  const resolution = number(MAP_METADATA_HEADERS.resolution_m)
  const originX = number(MAP_METADATA_HEADERS.origin_x)
  const originY = number(MAP_METADATA_HEADERS.origin_y)
  const width = number(MAP_METADATA_HEADERS.width)
  const height = number(MAP_METADATA_HEADERS.height)
  const updatedAt = number(MAP_METADATA_HEADERS.updated_at)
  if (
    resolution === null ||
    resolution <= 0 ||
    originX === null ||
    originY === null ||
    width === null ||
    !Number.isInteger(width) ||
    width < 1 ||
    height === null ||
    !Number.isInteger(height) ||
    height < 1 ||
    updatedAt === null ||
    !Number.isInteger(updatedAt) ||
    updatedAt < 0
  ) {
    return null
  }
  return {
    resolution_m: resolution,
    origin_x: originX,
    origin_y: originY,
    width,
    height,
    updated_at: updatedAt,
  }
}

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
  const exactProfile =
    profile === 'c1_basic_control'
      ? C1_BASIC_CONTROL_INTENTS
      : profile === 'c2_fleet_operations'
        ? C2_FLEET_OPERATIONS_INTENTS
        : profile === 'supervised_vertical'
          ? SUPERVISED_VERTICAL_INTENTS
          : null
  return (
    exactProfile === null ||
    (enabled.every((name) => name === 'body_pulse' || name === 'robot_peripheral' || name === 'camera_control' || exactProfile.includes(name as ConsoleIntentName)) &&
      exactProfile.every((name) => enabled.includes(name)))
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

function isDeviceClass(value: unknown): value is DeviceClass {
  return typeof value === 'string' && (DEVICE_CLASSES as readonly string[]).includes(value)
}

/**
 * A relay without device classes omits `device_class` and `unit`; those
 * records are aircraft whose unit is the drone id. Present values are never
 * rewritten, so an invalid class still fails validation.
 */
export function normalizeRelayAircraftState(value: unknown): unknown {
  if (!isRecord(value)) return value
  const drone: Record<string, unknown> = { ...value }
  delete drone.client_observation
  if (!Object.hasOwn(drone, 'membership_history_truncated')) drone.membership_history_truncated = 0
  const nodeType = drone.node_type
  const deviceClass = drone.device_class
  if (nodeType === undefined) {
    drone.node_type = deviceClass === 'ground_vehicle' ? 'ground' : 'aircraft'
  } else if (nodeType !== 'aircraft' && nodeType !== 'ground') {
    return drone
  }
  const derivedClass: DeviceClass = drone.node_type === 'ground' ? 'ground_vehicle' : 'aircraft'
  if (deviceClass !== undefined && deviceClass !== derivedClass) {
    drone.device_class = 'invalid-node-type'
  } else {
    drone.device_class = derivedClass
  }
  if (!Object.hasOwn(drone, 'unit')) drone.unit = drone.drone_id
  return drone
}

export function isRelayAircraftState(value: unknown): value is RelayAircraftState {
  if (!isRecord(value)) return false
  const patterns = value.camera_patterns
  const readinessReasons = value.readiness_reasons

  return (
    isDroneId(value.drone_id) &&
    (value.node_type === 'aircraft' || value.node_type === 'ground') &&
    isDeviceClass(value.device_class) &&
    isDroneId(value.unit) &&
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
    isTelemetryState(value.telemetry) &&
    isProjectedReport(value.node_status, 'node_status', value.drone_id) &&
    isProjectedReport(value.camera_capabilities, 'capabilities', value.drone_id) &&
    Array.isArray(value.membership_history) &&
    isNonNegativeInteger(value.membership_history_truncated) &&
    isVideoStreamState(value.video) &&
    isDeviceCameras(value.cameras) &&
    isSensorState(value.sensor)
  )
}

function isTelemetryState(value: unknown): boolean {
  if (value === null) return true
  if (!isRecord(value)) return false
  return ['x', 'y', 'z', 'vx', 'vy', 'vz', 'yaw_deg', 'heading_deg'].every((key) => value[key] === undefined || isFiniteNumber(value[key])) &&
    ['battery', 'link', 'pos_quality'].every((key) => value[key] === undefined || isNullableUnitNumber(value[key])) &&
    (value.t === undefined || isNonNegativeInteger(value.t)) &&
    (value.state === undefined || typeof value.state === 'string')
}

function isProjectedReport(value: unknown, type: 'node_status' | 'capabilities', droneId: unknown): boolean {
  if (value === undefined || value === null) return true
  if (!isRecord(value) || value.v !== 1 || value.type !== type || value.drone_id !== droneId) return false
  if (type === 'node_status') return isProjectedNodeStatus(value)
  const frame = { ...value, event_id: 'projection', session: 'projection', connection_epoch: 1 }
  return isPublicCapabilitiesEvent(frame)
}

/** Bounded JSON mirrors the backend extension; never interpret values as commands or HTML. */
export function isDeviceTelemetry(value: unknown): value is DeviceTelemetry {
  let count = 0
  const visit = (item: unknown, depth: number): boolean => {
    count += 1
    if (depth > MAX_DEVICE_TELEMETRY_DEPTH || count > 4096) return false
    if (item === null || typeof item === 'boolean') return true
    if (typeof item === 'string') return Array.from(item).length <= 512
    if (typeof item === 'number') return Number.isFinite(item) && Math.abs(item) <= Number.MAX_SAFE_INTEGER
    if (Array.isArray(item)) return item.length <= 512 && item.every((entry) => visit(entry, depth + 1))
    return isRecord(item) && Object.keys(item).length <= 128 && Object.entries(item).every(([key, entry]) =>
      /^[a-z][a-z0-9_]{0,63}$/.test(key) && visit(entry, depth + 1))
  }
  return isRecord(value) && visit(value, 1) && canonicalJsonByteLength(value) <= MAX_DEVICE_TELEMETRY_BYTES
}

function isDeviceCameras(value: unknown): value is DeviceCameraState[] | undefined {
  if (value === undefined) return true
  if (!Array.isArray(value) || value.length > 8) return false
  const ids = new Set<string>()
  const streams = new Set<string>()
  for (const camera of value) {
    if (!isRecord(camera) || Object.keys(camera).length !== 5 ||
      typeof camera.camera_id !== 'string' || !/^[a-z][a-z0-9_-]{0,31}$/.test(camera.camera_id) ||
      typeof camera.label !== 'string' || [...camera.label].length < 1 || [...camera.label].length > 64 ||
      camera.label.trim() !== camera.label || /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/u.test(camera.label) || /[^\S ]/u.test(camera.label) ||
      !validMediaStreamName(camera.stream) || !isVideoStreamState({ status: camera.status, last_frame_at: camera.last_frame_at }) ||
      ids.has(camera.camera_id) || streams.has(camera.stream)) return false
    ids.add(camera.camera_id)
    streams.add(camera.stream)
  }
  return true
}

function isSensorState(value: unknown): value is SensorState | undefined {
  if (value === undefined) return true
  if (!isRecord(value)) return false
  return (
    Object.keys(value).length === 2 &&
    (value.kind === null || value.kind === 'lidar_scan') &&
    (value.last_scan_at === null || isNonNegativeInteger(value.last_scan_at))
  )
}

function isSensorPose(value: unknown): value is SensorPose {
  if (!isRecord(value)) return false
  return (
    Object.keys(value).length === 3 &&
    isFiniteNumber(value.x) &&
    isFiniteNumber(value.y) &&
    isFiniteNumber(value.yaw_deg) &&
    value.yaw_deg >= 0 &&
    value.yaw_deg < 360
  )
}

function isSensorRanges(value: unknown, increment: SensorAngleIncrement): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === 360 / increment &&
    value.length <= MAX_SENSOR_RANGES &&
    value.every(
      (range) => Number.isInteger(range) && Number(range) >= 0 && Number(range) <= MAX_SENSOR_RANGE_CM,
    )
  )
}

/** The relay's canonical JSON: sorted keys, no whitespace, UTF-8 bytes. */
function canonicalJsonByteLength(value: unknown): number {
  const canonical = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(canonical)
    if (isRecord(item)) {
      return Object.fromEntries(
        Object.keys(item)
          .sort()
          .map((key) => [key, canonical(item[key])]),
      )
    }
    return item
  }
  return new TextEncoder().encode(JSON.stringify(canonical(value))).length
}

/** Mirrors the relay's parse_sensor bounds; a frame outside them fails closed. */
function isSensorEvent(value: Record<string, unknown>): boolean {
  const increment = value.angle_increment_deg
  if (!(SENSOR_ANGLE_INCREMENTS as readonly unknown[]).includes(increment)) return false
  return (
    isDroneId(value.drone_id) &&
    isNonNegativeInteger(value.connection_epoch) &&
    value.kind === 'lidar_scan' &&
    isSensorPose(value.pose) &&
    isFiniteNumber(value.angle_min_deg) &&
    isFiniteNumber(value.range_min_m) &&
    isFiniteNumber(value.range_max_m) &&
    value.range_min_m >= 0 &&
    value.range_max_m > value.range_min_m &&
    isSensorRanges(value.ranges_cm, increment as SensorAngleIncrement) &&
    canonicalJsonByteLength(value) <= MAX_SENSOR_FRAME_BYTES
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

const PUBLIC_NODE_ENVELOPE_FIELDS = ['v', 't', 'type', 'event_id', 'session', 'drone_id', 'connection_epoch']

function hasPublicNodeEnvelope(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return hasExactFields(value, [...PUBLIC_NODE_ENVELOPE_FIELDS, ...fields]) &&
    Number.isSafeInteger(value.t) && Number(value.t) >= 0 &&
    Number.isSafeInteger(value.drone_id) && Number(value.drone_id) > 0 &&
    Number.isSafeInteger(value.connection_epoch) && Number(value.connection_epoch) > 0 &&
    isBoundedNodeText(value.event_id) && isBoundedNodeText(value.session)
}

function isBoundedNodeText(value: unknown): value is string {
  return typeof value === 'string' && Array.from(value).length > 0 && Array.from(value).length <= 512
}

function isCanonicalNodeText(value: unknown): value is string {
  return isCanonicalIntentText(value, 512) && new TextEncoder().encode(value).length <= 512
}

function isNullableNodeText(value: unknown): value is string | null {
  return value === null || isBoundedNodeText(value)
}

function isAzimuth(value: unknown): value is number {
  return isFiniteNumber(value) && value >= 0 && value < 360
}

function isPublicCapabilitiesEvent(value: Record<string, unknown>): boolean {
  const texts = ['aircraft_model', 'aircraft_firmware', 'rc_firmware', 'phone_model', 'android_version', 'sdk_version']
  const modes = value.native_panorama_modes
  return hasPublicNodeEnvelope(value, [
    'native_panorama_modes', 'photo_capture', 'gimbal_pitch_min_deg', 'gimbal_pitch_max_deg',
    'horizontal_fov_deg', 'storage_remaining_bytes', 'media_retrieval', ...texts, 'measured_hfov_deg',
  ]) && Array.isArray(modes) && modes.length <= 64 && modes.every(isCanonicalNodeText) &&
    new Set(modes).size === modes.length && new TextEncoder().encode(JSON.stringify(modes)).length <= 8192 &&
    typeof value.photo_capture === 'boolean' && typeof value.media_retrieval === 'boolean' &&
    isFiniteNumber(value.gimbal_pitch_min_deg) && isFiniteNumber(value.gimbal_pitch_max_deg) &&
    value.gimbal_pitch_min_deg < value.gimbal_pitch_max_deg &&
    isFiniteNumber(value.horizontal_fov_deg) && value.horizontal_fov_deg > 0 && value.horizontal_fov_deg <= 360 &&
    Number.isSafeInteger(value.storage_remaining_bytes) && Number(value.storage_remaining_bytes) >= 0 &&
    texts.every((field) => isCanonicalNodeText(value[field])) &&
    (value.measured_hfov_deg === null || (isFiniteNumber(value.measured_hfov_deg) && value.measured_hfov_deg > 0 && value.measured_hfov_deg < 180))
}

function isPublicNodeStatusEvent(value: Record<string, unknown>): boolean {
  const optional = [
    ...(Object.hasOwn(value, 'device_telemetry') ? ['device_telemetry'] : []),
    ...(Object.hasOwn(value, 'local_height') ? ['local_height'] : []),
  ]
  return hasPublicNodeEnvelope(value, ['virtual_stick_enabled', 'control_authority', 'authority_change_reason',
    'watchdog_state', 'video_publish_state', 'phone_battery_percent', 'phone_thermal_state', ...optional]) &&
    (!Object.hasOwn(value, 'device_telemetry') || isDeviceTelemetry(value.device_telemetry)) &&
    (!Object.hasOwn(value, 'local_height') || isLocalHeight(value.local_height)) &&
    typeof value.virtual_stick_enabled === 'boolean' && typeof value.control_authority === 'boolean' &&
    (value.authority_change_reason === null || (isBoundedNodeText(value.authority_change_reason) && /^[a-z0-9_]+$/.test(value.authority_change_reason))) &&
    (value.watchdog_state === 'nominal' || value.watchdog_state === 'hold' || value.watchdog_state === 'failsafe') &&
    (value.video_publish_state === 'stopped' || value.video_publish_state === 'connecting' || value.video_publish_state === 'publishing' || value.video_publish_state === 'failed') &&
    Number.isInteger(value.phone_battery_percent) && Number(value.phone_battery_percent) >= 0 && Number(value.phone_battery_percent) <= 100 &&
    typeof value.phone_thermal_state === 'string' && ['none', 'light', 'moderate', 'severe', 'critical', 'emergency', 'shutdown'].includes(value.phone_thermal_state)
}

function isLocalHeight(value: unknown): value is RelayLocalHeight {
  if (!isRecord(value)) return false
  return hasExactFields(value, ['z_m', 'source', 'age_ms']) &&
    isFiniteNumber(value.z_m) &&
    value.source === 'flight_controller_altitude' &&
    isNonNegativeInteger(value.age_ms)
}

function isProjectedNodeStatus(value: Record<string, unknown>): boolean {
  const frame: Record<string, unknown> = {
    ...value,
    event_id: 'projection',
    session: 'projection',
    connection_epoch: 1,
  }
  const localHeight = frame.local_height
  delete frame.local_height
  return isPublicNodeStatusEvent(frame) &&
    (localHeight === undefined || localHeight === null || isProjectedLocalHeight(localHeight))
}

function isProjectedLocalHeight(value: unknown): value is RelayProjectedLocalHeight {
  return isRecord(value) &&
    hasExactFields(value, ['z_m', 'source', 'age_ms', 'reported_at_ms']) &&
    isFiniteNumber(value.z_m) &&
    value.source === 'flight_controller_altitude' &&
    isNonNegativeInteger(value.age_ms) &&
    isNonNegativeInteger(value.reported_at_ms)
}

function isPublicCaptureReadinessEvent(value: Record<string, unknown>): boolean {
  const booleans = ['pose_ok', 'clearance_ok', 'camera_ok', 'storage_ok', 'motion_ok', 'image_quality_ok']
  const delta = value.suggested_delta
  return hasPublicNodeEnvelope(value, ['room_id', 'capture_id', 'guidance_mode', 'pose_source', ...booleans,
    'coverage_missing', 'next_heading_deg', 'suggested_delta']) &&
    isNullableNodeText(value.room_id) && isNullableNodeText(value.capture_id) &&
    (value.guidance_mode === 'visual_advisory' || value.guidance_mode === 'registered_metric') && isBoundedNodeText(value.pose_source) &&
    booleans.every((field) => typeof value[field] === 'boolean') &&
    Array.isArray(value.coverage_missing) && value.coverage_missing.length <= 8 && value.coverage_missing.every(isAzimuth) &&
    new Set(value.coverage_missing).size === value.coverage_missing.length &&
    (value.next_heading_deg === null || isAzimuth(value.next_heading_deg)) &&
    (delta === null || (isRecord(delta) && hasExactFields(delta, ['kind', 'degrees']) &&
      (delta.kind === 'yaw' || delta.kind === 'gimbal') && isFiniteNumber(delta.degrees)))
}

/** Parses the M1.1 event seam; unknown frames fail closed. */
export function parseRelayServerEvent(value: unknown): RelayServerEvent | null {
  const observation = parseObservation(value)
  if (observation) return observation
  if (!isRecord(value) || !hasBaseEvent(value) || typeof value.type !== 'string') return null

  if (value.type === 'capabilities') return isPublicCapabilitiesEvent(value) ? value as unknown as RelayCapabilitiesEvent : null
  if (value.type === 'node_status') return isPublicNodeStatusEvent(value) ? value as unknown as RelayNodeStatusEvent : null
  if (value.type === 'capture_readiness') return isPublicCaptureReadinessEvent(value) ? value as unknown as RelayCaptureReadinessEvent : null

  if (value.type === 'state') {
    const drones = Array.isArray(value.drones)
      ? value.drones.map(normalizeRelayAircraftState)
      : value.drones
    if (
      !isNonNegativeInteger(value.roster_version) ||
      (value.state_sequence !== undefined &&
        (!Number.isSafeInteger(value.state_sequence) || Number(value.state_sequence) < 1)) ||
      typeof value.armed !== 'boolean' ||
      typeof value.estop !== 'boolean' ||
      !isDroneIds(value.selection) ||
      !(
        value.formation === 'none' ||
        FORMATION_NAME_SET.has(value.formation as FormationName)
      ) ||
      !isFiniteNumber(value.spacing) ||
      typeof value.mode !== 'string' ||
      !isCapabilityAdvertisement(value.capability_profile, value.enabled_intent_names) ||
      !isNullableRecord(value.pending) ||
      !isNullableRecord(value.accepted_plan) ||
      !Array.isArray(drones) ||
      drones.length > MAX_FLEET_DEVICES ||
      !drones.every(isRelayAircraftState) ||
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
    return { ...value, drones } as unknown as RelayStateEvent
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
      !(value.node_type === undefined || value.node_type === 'aircraft' || value.node_type === 'ground') ||
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
    return { ...value, node_type: value.node_type ?? 'aircraft' } as unknown as RelayMembershipEvent
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

  if (value.type === 'sensor') {
    if (!isSensorEvent(value)) return null
    return value as unknown as RelaySensorEvent
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
  if (value.source === 'webcam' && name === 'arm' && !value.confirm) return false
  return hasValidSelection(name, selection, value.args)
}

const FORMATION_NAME_SET: ReadonlySet<FormationName> = new Set(FORMATION_NAMES)

/** Mirrors relay/intent_v1.py _parse_args for the console-built subset. */
function hasValidArgs(name: ConsoleIntentName, args: Record<string, unknown>): boolean {
  const keys = Object.keys(args)
  switch (name) {
    case 'camera_control':
      return isCameraControlArgs(args)
    case 'robot_peripheral':
      return isRobotPeripheralArgs(args)
    case 'body_pulse':
      return keys.length === 2 && Number.isSafeInteger(args.forward_mm_s) &&
        Number(args.forward_mm_s) !== 0 && Math.abs(Number(args.forward_mm_s)) <= 250 &&
        Number.isSafeInteger(args.duration_ms) && Number(args.duration_ms) >= 100 &&
        Number(args.duration_ms) <= 500
    case 'select':
      return keys.length === 1 && isIntentDroneIds(args.ids) && args.ids.length > 0
    case 'translate':
      return keys.length === 2 && isFiniteNumber(args.dx) && isFiniteNumber(args.dy)
    case 'altitude':
    case 'spacing':
      return keys.length === 1 && isFiniteNumber(args.delta)
    case 'formation_set':
      return keys.length === 1 && FORMATION_NAME_SET.has(args.name as FormationName)
    case 'sweep':
      return keys.length === 0 || (keys.length === 1 && isSweepBox(args.box))
    case 'capture_room':
      return (
        keys.length === 3 &&
        isCanonicalIntentText(args.room_id, MAX_INTENT_IDENTIFIER_CODE_POINTS) &&
        isCanonicalIntentText(args.capture_id, MAX_INTENT_IDENTIFIER_CODE_POINTS) &&
        CAPTURE_PATTERNS.has(args.pattern as CapturePattern)
      )
    case 'ground_velocity':
      return keys.length === 3 &&
        Number.isInteger(args.linear_mm_s) && Number(args.linear_mm_s) >= 0 && Number(args.linear_mm_s) <= 180 &&
        Number.isInteger(args.angular_mrad_s) && Math.abs(Number(args.angular_mrad_s)) <= 785 &&
        Number.isInteger(args.duration_ms) && Number(args.duration_ms) > 0 && Number(args.duration_ms) <= 500 &&
        ((args.linear_mm_s === 0) !== (args.angular_mrad_s === 0))
    case 'survey_area':
      return keys.length === 1 && isCanonicalIntentText(args.area_id, MAX_INTENT_IDENTIFIER_CODE_POINTS)
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
function hasValidSelection(
  name: ConsoleIntentName,
  selection: DroneId[],
  args: Record<string, unknown>,
): boolean {
  if (name === 'formation_next') return selection.length >= 2
  if (name === 'formation_set') {
    const minimum = args.name === 'wedge' || args.name === 'diamond' ? 4 : 2
    return selection.length >= minimum
  }
  switch (SELECTION_RULES[name]) {
    case 'any':
    case 'all':
      return true
    case 'fleet':
      return selection.length === 0
    case 'at least one':
    case 'selected':
      return selection.length > 0
    case 'connected device':
    case 'exactly one':
      return selection.length === 1
  }
}
