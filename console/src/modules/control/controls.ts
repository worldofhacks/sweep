import { membershipWord, motionObservationCurrent, observationCurrent } from '../../control/observation'
/**
 * Pure derivations for the Control module, lifted from the Sweep Console v4
 * design's controlSpec, dpad, catalog, slots, captureBlock and flow. Every
 * input is authoritative relay state; nothing here invents a value.
 */
import { flightActionBlockedReason } from '../../gesture/flight'
import type { ControlState, DeviceLabeller, RequestRecord, RequestStatus } from '../../control/state'
import {
  capabilityBlockedReason,
  deviceLabeller,
  formatDeviceId,
  formatDroneId,
  pluralNoun,
  rosterNoun,
  selectionNoun,
} from '../../control/state'
import type { TranslateDirection } from '../../control/intent'
import type {
  ConsoleIntentName,
  DroneId,
  FormationName,
  IntentArgs,
  IntentArgsByName,
  SelectionRule,
  RelayAircraftState,
} from '../../relay/contract'
import {
  FORMATION_NAMES as CONTRACT_FORMATION_NAMES,
  followsSelection,
  isSupportedIntent,
  requiresConfirmation,
  selectionRule,
} from '../../relay/contract'
import { isLinkUp, isReady, sortedAircraft, type Tone } from '../../shell/derive'

/** One control press: the intent it drafts and the aircraft it addresses. */
export interface ControlPress<N extends ConsoleIntentName = ConsoleIntentName> {
  name: N
  args: IntentArgsByName[N]
  /** Omitted: the authoritative selection. `land_all` names the whole roster. */
  targets?: DroneId[]
}

export type ControlBadge = '' | 'confirm' | 'unsupported'

export interface ControlSpec {
  key: string
  label: string
  name: ConsoleIntentName
  press: ControlPress
  confirm: boolean
  supported: boolean
  /** A press drafts or sends. Capability-disabled controls always remain inert. */
  enabled: boolean
  badge: ControlBadge
  /** The one sentence under the control: the blocking reason, the refusal copy, or what a press does. */
  note: string
  noteTone: 'warn' | 'muted'
  rule: SelectionRule
}

export const FORMATION_NAMES = CONTRACT_FORMATION_NAMES
const FORMATION_SPACING_CLEARANCE_FACTOR = 1.01

export function connectionReason(state: ControlState): string | null {
  return isLinkUp(state.connection.status)
    ? null
    : `The console connection is ${state.connection.status}. Nothing can be sent.`
}

export const STOP_ACTIVE_REASON =
  'The network stop is active. Motion intents are refused until the relay reports it clear.'
/** The aircraft wording; `noSelectionReason` and `noReadyReason` follow the roster's classes. */
export const NO_SELECTION_REASON = 'No aircraft selected.'
export const NO_READY_REASON = 'No aircraft is ready.'

export function noSelectionReason(state: ControlState): string {
  return `No ${pluralNoun(rosterNoun(sortedAircraft(state.aircraft)))} selected.`
}

export function noReadyReason(state: ControlState): string {
  return `No ${rosterNoun(sortedAircraft(state.aircraft))} is ready.`
}

/** Intents the relay refuses for ground vehicles with `unsupported_for_device_class`. */
export const AIRCRAFT_ONLY_INTENTS: readonly ConsoleIntentName[] = [
  'body_pulse',
  'takeoff',
  'land',
  'land_all',
  'altitude',
  'sweep',
  'capture_room',
]
export const ROBOT_UNSUPPORTED_NOTE = 'Not available for robots.'

/** True when the ids name at least one roster device and every one is a ground vehicle. */
export function allGroundVehicles(state: ControlState, ids: readonly DroneId[]): boolean {
  const devices = ids.flatMap((id) => (state.aircraft[id] ? [state.aircraft[id]] : []))
  return devices.length > 0 && devices.every((device) => device.device_class === 'ground_vehicle')
}

/**
 * The note for an aircraft-only intent whose every target is a ground vehicle.
 * A mixed set keeps the control enabled: the relay decides per device and
 * `land_all` skips ground vehicles rather than refusing.
 */
export function deviceClassBlockedReason(
  state: ControlState,
  name: ConsoleIntentName,
  targets: readonly DroneId[] = state.selection,
): string | null {
  if (!AIRCRAFT_ONLY_INTENTS.includes(name)) return null
  if (name === 'body_pulse' && targets.some((id) => state.aircraft[id]?.device_class === 'ground_vehicle')) {
    return 'Body pulses require an aircraft-only selection.'
  }
  return allGroundVehicles(state, targets) ? ROBOT_UNSUPPORTED_NOTE : null
}

export function readyIds(state: ControlState): DroneId[] {
  return sortedAircraft(state.aircraft)
    .filter(isReady)
    .map((drone) => drone.drone_id)
}

export function rosterIds(state: ControlState): DroneId[] {
  return sortedAircraft(state.aircraft).map((drone) => drone.drone_id)
}

function notReadySentence(state: ControlState): string | null {
  const notReady = state.selection.filter((id) => !isReady(state.aircraft[id]))
  if (notReady.length === 0) return null
  const label = deviceLabeller(state.aircraft)
  return `${notReady.map(label).join(', ')} ${notReady.length > 1 ? 'are' : 'is'} not ready.`
}

interface GateOptions {
  /** The rule needs at least one selected device. */
  sel?: boolean
  /** Every selected device must be ready. */
  ready?: boolean
  /** Reason that applies before the capability check, for select-all with nothing ready. */
  extra?: string | null
  okNote?: string
  /** The devices the press addresses when not the selection, for the class gate. */
  targets?: readonly DroneId[]
}

interface Gate {
  reason: string | null
  /** The intent is refused for every target's device class; shown as unsupported. */
  unsupported: boolean
}

/**
 * Reason order: connection, authoritative relay capability, device class,
 * selection rule, then the local implementation set and network stop.
 * Capability metadata is authoritative; unsupported names stay visible but
 * never draft or dispatch.
 */
export function gateControl(state: ControlState, name: ConsoleIntentName, options: GateOptions = {}): Gate {
  const connection = connectionReason(state)
  if (connection) return { reason: connection, unsupported: false }
  const capability = capabilityBlockedReason(state, name)
  if (capability) return { reason: capability, unsupported: false }
  const deviceClass = deviceClassBlockedReason(state, name, options.targets)
  if (deviceClass) return { reason: deviceClass, unsupported: true }
  if (options.sel && state.selection.length === 0) return { reason: noSelectionReason(state), unsupported: false }
  if (options.ready) {
    const notReady = notReadySentence(state)
    if (notReady) return { reason: notReady, unsupported: false }
  }
  if (options.sel && name !== 'hold' && name !== 'land' && state.selection.some((id) => !motionObservationCurrent(state.aircraft[id]))) {
    return { reason: 'Current target motion telemetry is unavailable. Wait for a fresh report.', unsupported: false }
  }
  if (name === 'body_pulse') {
    const reason = flightActionBlockedReason(state, null, { kind: 'draft', name: 'body_pulse', direction: 'forward' })
    if (reason) return { reason, unsupported: false }
  }
  if (options.extra) return { reason: options.extra, unsupported: false }
  if (!isSupportedIntent(name)) {
    return { reason: `${name} is not implemented by this console.`, unsupported: true }
  }
  if (state.estop && name !== 'estop' && name !== 'land' && name !== 'land_all') {
    return { reason: STOP_ACTIVE_REASON, unsupported: false }
  }
  return { reason: null, unsupported: false }
}

function control(
  state: ControlState,
  key: string,
  label: string,
  press: ControlPress,
  options: GateOptions = {},
): ControlSpec {
  const { name } = press
  const confirm = requiresConfirmation(name)
  const gate = gateControl(state, name, { ...options, targets: options.targets ?? press.targets })
  const supported = isSupportedIntent(name) && !gate.unsupported
  const enabled = gate.reason === null
  const note =
    gate.reason ??
    options.okNote ??
    (confirm ? 'Confirmation required before send.' : 'Sends immediately on the console connection.')
  return {
    key,
    label,
    name,
    press,
    confirm,
    supported,
    enabled,
    badge: supported ? (confirm ? 'confirm' : '') : 'unsupported',
    note,
    noteTone: gate.reason ? 'warn' : 'muted',
    rule: selectionRule(name),
  }
}

/** Swarm › Fleet: Arm, Disarm, Select all ready. */
export function fleetControls(state: ControlState): ControlSpec[] {
  const ready = readyIds(state)
  return [
    control(state, 'arm', 'Arm', { name: 'arm', args: {} }),
    control(
      state,
      'disarm',
      'Disarm',
      { name: 'disarm', args: {} },
      {
        okNote:
          'Withdraws session arm authorization only after the relay proves the fleet grounded; it does not command the aircraft.',
      },
    ),
    control(
      state,
      'select-all',
      'Select all ready',
      { name: 'select', args: { ids: ready }, targets: ready },
      {
        extra: ready.length === 0 ? noReadyReason(state) : null,
        okNote: `Selects every ready ${rosterNoun(sortedAircraft(state.aircraft))}.`,
      },
    ),
  ]
}

/** Swarm › Motion: every selected device. */
export function motionControls(state: ControlState): ControlSpec[] {
  return [
    control(state, 'takeoff', 'Takeoff', { name: 'takeoff', args: {} }, { sel: true, ready: true }),
    control(state, 'hold', 'Hold', { name: 'hold', args: {} }, { sel: true, ready: true }),
    control(state, 'come_home', 'Come home', { name: 'come_home', args: {} }, { sel: true, ready: true }),
    control(
      state,
      'land_all',
      'Land all',
      { name: 'land_all', args: {}, targets: rosterIds(state) },
      { okNote: 'Confirmation required. Targets every aircraft in the roster.' },
    ),
    control(state, 'sweep', 'Sweep', { name: 'sweep', args: {} }, { sel: true }),
    control(state, 'spacing-', 'Spacing tighter', { name: 'spacing', args: { delta: -1 } }, { sel: true }),
    control(state, 'spacing+', 'Spacing wider', { name: 'spacing', args: { delta: 1 } }, { sel: true }),
    control(
      state,
      'formation_next',
      'Formation next',
      { name: 'formation_next', args: {} },
      { sel: true, extra: classFormationReason(state) },
    ),
  ]
}

export const MOTION_FOOTNOTE =
  'Motion controls use the authoritative selection. Robot steps resolve against the room frame; aircraft use the relay-configured frame. Every target remains subject to the arbiter.'

/** Commands: the four MVP formations and the two altitude steps. */
export function formationControls(state: ControlState): ControlSpec[] {
  return FORMATION_NAMES.map((name) =>
    control(
      state,
      `formation-${name}`,
      name,
      { name: 'formation_set', args: { name } },
      {
        sel: true,
        extra: classFormationReason(state, name),
      },
    ),
  )
}

/** Classes form independently; a singleton holds its pose in a mixed formation. */
export function classFormationReason(state: ControlState, name?: FormationName): string | null {
  const devices = state.selection.flatMap((id) => state.aircraft[id] ? [state.aircraft[id]] : [])
  const classes = [...new Set(devices.map((device) => device.device_class))]
  if (classes.length < 2) {
    const reason = name ? formationSelectionReason(name, devices.length) : formationCountReason(devices.length)
    return devices[0]?.device_class === 'ground_vehicle' ? reason?.replaceAll('aircraft', 'robots') ?? null : reason
  }
  for (const deviceClass of classes) {
    const count = devices.filter((device) => device.device_class === deviceClass).length
    if (count === 1) continue
    const reason = name ? formationSelectionReason(name, count) : formationCountReason(count)
    if (reason) return `${deviceClass === 'aircraft' ? 'Aircraft' : 'Robot'} group: ${reason.replaceAll('aircraft', 'devices')}`
  }
  return null
}

function formationSelectionReason(name: FormationName, count: number): string | null {
  const minimum = name === 'wedge' || name === 'diamond' ? 4 : 2
  return formationCountReason(count, minimum, name)
}

function formationCountReason(
  count: number,
  minimum = 2,
  name?: FormationName,
): string | null {
  const subject = name === undefined ? 'formation' : `${name} formation`
  if (count < minimum) return `${subject} requires at least ${minimum} selected aircraft.`
  if (count > 6) {
    return `formation supports at most 6 selected aircraft.`
  }
  return null
}

export function altitudeControls(state: ControlState): ControlSpec[] {
  return [
    control(state, 'altitude+', 'Altitude up', { name: 'altitude', args: { delta: 1 } }, { sel: true }),
    control(state, 'altitude-', 'Altitude down', { name: 'altitude', args: { delta: -1 } }, { sel: true }),
  ]
}

export type CatalogStatus = 'available' | 'unsupported' | 'later'

export interface CatalogRow {
  key: string
  label: string
  intent: string
  confirm: 'confirm' | '—'
  rule: string
  status: CatalogStatus
  note: string
  noteTone: 'warn' | 'muted'
  enabled: boolean
  spec: ControlSpec | null
}

export interface CatalogGroup {
  title: string
  rows: CatalogRow[]
}

function catalogRow(spec: ControlSpec): CatalogRow {
  return {
    key: spec.key,
    label: spec.label,
    intent: spec.name,
    confirm: spec.confirm ? 'confirm' : '—',
    rule: spec.rule,
    status: spec.supported ? 'available' : 'unsupported',
    note: spec.note,
    noteTone: spec.noteTone,
    enabled: spec.enabled,
    spec,
  }
}

/** Rows for survey_area and map_area: the console does not build these envelopes yet. */
function laterRow(key: string, label: string, intent: string, rule: string): CatalogRow {
  return {
    key,
    label,
    intent,
    confirm: 'confirm',
    rule,
    status: 'later',
    note: `${intent} needs an area_id from the map module, which this console does not build yet.`,
    noteTone: 'muted',
    enabled: false,
    spec: null,
  }
}

/** The command catalogue: Fleet and Motion, in the design's order. */
export function commandCatalog(state: ControlState): CatalogGroup[] {
  const [arm, disarm, selectAll] = fleetControls(state)
  const motion = Object.fromEntries(motionControls(state).map((spec) => [spec.key, spec]))
  return [
    {
      title: 'Fleet',
      rows: [arm, disarm, { ...selectAll, label: 'Select all' }].map(catalogRow),
    },
    {
      title: 'Motion',
      rows: [
        catalogRow(motion.takeoff),
        catalogRow(motion.hold),
        catalogRow(control(state, 'body-forward', 'Forward 0.5 seconds', { name: 'body_pulse', args: { forward_mm_s: 250, duration_ms: 500 } }, { sel: true, ready: true })),
        catalogRow(control(state, 'body-backward', 'Backward 0.5 seconds', { name: 'body_pulse', args: { forward_mm_s: -250, duration_ms: 500 } }, { sel: true, ready: true })),
        catalogRow(motion.come_home),
        catalogRow(control(state, 'land', 'Land', { name: 'land', args: {} }, { sel: true })),
        catalogRow(motion.land_all),
        catalogRow(motion.formation_next),
        catalogRow(motion['spacing-']),
        catalogRow(motion['spacing+']),
        catalogRow(motion.sweep),
        laterRow('survey_area', 'Survey area', 'survey_area', 'any'),
        laterRow('map_area', 'Map area', 'map_area', 'non-empty'),
      ],
    },
  ]
}

export interface DpadCell {
  key: string
  label: string
  aria: string
  direction: TranslateDirection | null
}

/** Nine cells, row by row: north on top, west and east beside the centre, south below. */
export const DPAD_CELLS: readonly DpadCell[] = [
  { key: 'nw', label: '', aria: 'spacer', direction: null },
  { key: 'n', label: '↑', aria: 'Translate north', direction: 'north' },
  { key: 'ne', label: '', aria: 'spacer', direction: null },
  { key: 'w', label: '←', aria: 'Translate west', direction: 'west' },
  { key: 'c', label: '·', aria: 'centre', direction: null },
  { key: 'e', label: '→', aria: 'Translate east', direction: 'east' },
  { key: 'sw', label: '', aria: 'spacer', direction: null },
  { key: 's', label: '↓', aria: 'Translate south', direction: 'south' },
  { key: 'se', label: '', aria: 'spacer', direction: null },
]

/** Translate pad reason order from the design: connection, stop, selection. */
export function dpadBlockedReason(state: ControlState): string | null {
  return (
    connectionReason(state) ??
    capabilityBlockedReason(state, 'translate') ??
    (state.estop ? STOP_ACTIVE_REASON : state.selection.length === 0 ? noSelectionReason(state) : null) ??
    (state.selection.some((id) => !motionObservationCurrent(state.aircraft[id])) ? 'Current target motion telemetry is unavailable. Wait for a fresh report.' : null)
  )
}

/** Planner slot positions in metres, mirroring planner/planner.py exactly. */
export function formationSlots(name: string, count: number, spacing: number): Array<[number, number]> {
  if (
    !FORMATION_NAMES.includes(name as FormationName) ||
    !Number.isInteger(count) ||
    count < 2 ||
    count > 6 ||
    !Number.isFinite(spacing) ||
    spacing <= 0 ||
    ((name === 'wedge' || name === 'diamond') && count < 4)
  ) {
    return []
  }
  const n = count
  let raw: Array<[number, number]>
  if (name === 'line') {
    raw = Array.from({ length: n }, (_, index) => [index - (n - 1) / 2, 0])
  } else if (name === 'column') {
    raw = Array.from({ length: n }, (_, index) => [0, index - (n - 1) / 2])
  } else if (name === 'wedge') {
    raw = []
    const firstRow = n % 2 === 0 ? 0.5 : 1
    if (n % 2 !== 0) raw.push([0, 0])
    for (let row = 0; row < Math.floor(n / 2); row += 1) {
      const distance = firstRow + row
      raw.push([-distance, -distance], [distance, -distance])
    }
  } else {
    raw = Array.from({ length: n }, (_, index) => diamondPerimeter((4 * index) / n))
  }
  return normalizeFormationOffsets(raw).map(([x, y]) => [x * spacing + 0, y * spacing + 0])
}

function diamondPerimeter(position: number): [number, number] {
  if (position < 1) return [position, 1 - position]
  if (position < 2) return [2 - position, 1 - position]
  if (position < 3) return [2 - position, position - 3]
  return [position - 4, position - 3]
}

function normalizeFormationOffsets(raw: Array<[number, number]>): Array<[number, number]> {
  const centerX = raw.reduce((total, [x]) => total + x / raw.length, 0)
  const centerY = raw.reduce((total, [, y]) => total + y / raw.length, 0)
  const centered = raw.map(([x, y]) => [x - centerX, y - centerY] as [number, number])
  if (centered.length === 1) return [[0, 0]]
  let minimum = Number.POSITIVE_INFINITY
  for (let first = 0; first < centered.length; first += 1) {
    for (let second = first + 1; second < centered.length; second += 1) {
      minimum = Math.min(
        minimum,
        Math.hypot(
          centered[first][0] - centered[second][0],
          centered[first][1] - centered[second][1],
        ),
      )
    }
  }
  const scale = FORMATION_SPACING_CLEARANCE_FACTOR / minimum
  return centered.map(([x, y]) => [x * scale + 0, y * scale + 0])
}

export interface FormationDot {
  id: string
  left: string
  top: string
  slot: string
}

/**
 * Anonymous shape slots. The relay does not project planner assignments, so this
 * preview deliberately carries no aircraft identity. The plot is scale free; only
 * the metre labels need reported spacing.
 */
export function formationPlot(
  count: number,
  name: string | null,
  spacing: number | null,
): FormationDot[] {
  if (name === null || count === 0) return []
  const slots = formationSlots(name, count, spacing ?? 1)
  if (slots.length !== count) return []
  const span = Math.max(1.2, ...slots.map(([x, y]) => Math.max(Math.abs(x), Math.abs(y)))) * 2.4
  return slots.map(([x, y], i) => {
    return {
      id: `Slot ${i + 1}`,
      left: `${50 + (x / span) * 100}%`,
      top: `${50 + (y / span) * 100}%`,
      slot:
        spacing === null
          ? `slot ${i + 1} · spacing unreported`
          : `slot ${i + 1} · ${x.toFixed(1)} m, ${y.toFixed(1)} m`,
    }
  })
}

export function formationRelayNote(preview: string | null, reported: string | null): string {
  const shown = preview ?? reported
  if (reported === null) {
    return shown === null
      ? 'The relay has not reported a formation.'
      : `Requested ${shown}. Waiting for the relay to report the completed formation.`
  }
  if (shown === reported) return `The relay reports ${reported}.`
  return `Requested ${shown}. The relay still reports ${reported} until execution completes.`
}

export interface FanoutRow {
  id: string
  cmd: string
}

/** What the planner would propose per device for a pending, confirmation-gated draft. */
export function fanoutFor(
  name: ConsoleIntentName,
  args: IntentArgs,
  targets: DroneId[],
  label: DeviceLabeller = formatDroneId,
): FanoutRow[] {
  const pattern = 'pattern' in args ? args.pattern : 'the requested pattern'
  return targets.map((droneId, i) => {
    const id = label(droneId)
    switch (name) {
      case 'takeoff':
        return { id, cmd: 'take off to the indoor hover altitude, then hover' }
      case 'land':
      case 'land_all':
        return { id, cmd: 'land in place, motors off at touchdown' }
      case 'come_home':
        return { id, cmd: `goto(pad ${i + 1}) staggered ${i * 2} s, then land` }
      case 'sweep':
        return { id, cmd: `lane ${i + 1} of ${targets.length}, lawnmower pattern` }
      case 'capture_room':
        return { id, cmd: `hold, then capture ${pattern}` }
      case 'arm':
        return { id, cmd: 'arm(motors idle)' }
      case 'select':
        return { id, cmd: 'selection membership only, no motion' }
      default:
        return { id, cmd: name }
    }
  })
}

export interface ChipView {
  droneId: DroneId
  id: string
  sub: string
  selected: boolean
  selectable: boolean
  /** First readiness reason with the underscores spaced, or the membership word. */
  reason: string
}

/** The device's motion state word: the flight state, or the drive state for a ground vehicle. */
export function motionStateWord(drone: RelayAircraftState): string {
  if (!observationCurrent(drone)) return `${membershipWord(drone)} · ${drone.flight_state ? `last reported ${drone.flight_state}` : 'motion unreported'}`
  if (!motionObservationCurrent(drone)) return `current motion unknown · ${drone.flight_state ? `last reported ${drone.flight_state}` : 'motion unreported'}`
  if (drone.flight_state !== null) return drone.flight_state
  return drone.device_class === 'ground_vehicle' ? 'drive state unreported' : 'flight state unreported'
}

export function aircraftChips(state: ControlState): ChipView[] {
  return sortedAircraft(state.aircraft).map((drone) => {
    const selectCapability = capabilityBlockedReason(state, 'select')
    const selectable = selectCapability === null && isReady(drone)
    return {
      droneId: drone.drone_id,
      id: formatDeviceId(drone),
      sub: `${motionStateWord(drone)} · ${drone.battery === null ? '—' : `${Math.round(drone.battery * 100)}%`}`,
      selected: state.selection.includes(drone.drone_id),
      selectable,
      reason: selectCapability ?? (selectable
        ? ''
        : drone.readiness_reasons[0]
          ? drone.readiness_reasons[0].replaceAll('_', ' ')
          : drone.membership),
    }
  })
}

/** "D-03 telemetry stale · G-01 control authority missing", or empty when every device is ready. */
export function chipBlockers(state: ControlState): string {
  return sortedAircraft(state.aircraft)
    .filter((drone) => !isReady(drone))
    .map(
      (drone) =>
        `${formatDeviceId(drone)} ${(drone.readiness_reasons[0] ?? 'not selectable').replaceAll('_', ' ')}`,
    )
    .join(' · ')
}

/* Capture */

export type GuidanceMode = 'visual_advisory' | 'registered_metric'
export type SectorCoverage = 'accepted' | 'weak' | 'unseen' | 'unreported'
export type GateKey = 'pose' | 'clearance' | 'camera' | 'storage' | 'motion' | 'image_quality'

/**
 * The capture_readiness guidance mirror from the brief. No relay event carries
 * it on main yet, so the module renders every field as unreported until one does.
 */
export interface CaptureReadiness {
  guidance_mode: GuidanceMode
  pose_source: string
  pose_ok: boolean
  clearance_ok: boolean
  camera_ok: boolean
  storage_ok: boolean
  motion_ok: boolean
  image_quality_ok: boolean
  /** Eight azimuth sectors of 45°, the first centred on north. */
  coverage: SectorCoverage[]
  next_heading_deg: number | null
  suggested_delta: string | null
}

export const GATE_KEYS: readonly GateKey[] = ['pose', 'clearance', 'camera', 'storage', 'motion', 'image_quality']

export interface GateRow {
  key: GateKey
  word: 'pass' | 'fail' | 'unreported'
  tone: 'ok' | 'danger' | 'muted'
}

export function gateRows(guidance: CaptureReadiness | null): GateRow[] {
  return GATE_KEYS.map((key) => {
    if (!guidance) return { key, word: 'unreported', tone: 'muted' }
    const pass = guidance[`${key}_ok`]
    return { key, word: pass ? 'pass' : 'fail', tone: pass ? 'ok' : 'danger' }
  })
}

export function failingGates(guidance: CaptureReadiness): GateKey[] {
  return GATE_KEYS.filter((key) => !guidance[`${key}_ok`])
}

export interface SectorView {
  index: number
  rotation: string
  coverage: SectorCoverage | 'unreported'
}

export function compassSectors(guidance: CaptureReadiness | null): SectorView[] {
  return Array.from({ length: 8 }, (_, index) => ({
    index,
    rotation: `translateX(-50%) rotate(${index * 45}deg)`,
    coverage: guidance?.coverage[index] ?? 'unreported',
  }))
}

export function sectorSummary(guidance: CaptureReadiness | null): string {
  if (!guidance) return 'coverage unreported'
  const count = (value: SectorCoverage) => guidance.coverage.filter((sector) => sector === value).length
  return `${count('unseen')} unseen, ${count('weak')} weak, ${count('accepted')} accepted${count('unreported') ? `, ${count('unreported')} unreported` : ''}`
}

export function guidanceNote(guidance: CaptureReadiness | null): string {
  if (!guidance) {
    return 'No capture_readiness report has arrived. Gates, coverage and headings stay unreported rather than invented.'
  }
  return guidance.guidance_mode === 'visual_advisory'
    ? 'visual_advisory: guidance suggests yaw and gimbal only. No XYZ move is suggested in this mode.'
    : 'registered_metric: metric moves are available.'
}

export const ROOM_RULE = 'Lower-case letters, digits and hyphens, 3 to 24 characters.'

export interface CaptureGate {
  ready: boolean
  text: string
}

/**
 * The readiness sentence above Capture room, in the design's order: connection,
 * stop, exactly one selected, that one ready, room id, advertised pattern,
 * motion gate, then the all-clear.
 */
export function captureGate(
  state: ControlState,
  roomId: string,
  roomOk: boolean,
  guidance: CaptureReadiness | null,
): CaptureGate {
  if (!isLinkUp(state.connection.status)) {
    return { ready: false, text: `The console connection is ${state.connection.status}. Capture room cannot be sent.` }
  }
  const capability = capabilityBlockedReason(state, 'capture_room')
  if (capability) return { ready: false, text: capability }
  if (state.estop) {
    return { ready: false, text: 'The network stop is active. Capture room is refused until the relay reports it clear.' }
  }
  if (state.selection.length !== 1) {
    return {
      ready: false,
      text: `capture_room needs exactly one aircraft selected. ${state.selection.length} selected.`,
    }
  }
  const drone = state.aircraft[state.selection[0]]
  const id = deviceLabeller(state.aircraft)(state.selection[0])
  if (drone?.device_class === 'ground_vehicle') {
    return { ready: false, text: ROBOT_UNSUPPORTED_NOTE }
  }
  if (!isReady(drone)) {
    const reasons = drone?.readiness_reasons.length ? drone.readiness_reasons.join(', ') : 'not selectable'
    return { ready: false, text: `${id} is not ready: ${reasons}.` }
  }
  if (!roomOk) {
    return {
      ready: false,
      text: 'The room identifier must be lower-case letters, digits and hyphens, 3 to 24 characters.',
    }
  }
  if (!drone.camera_patterns.includes(state.capturePattern)) {
    return { ready: false, text: `${id} does not advertise ${state.capturePattern}.` }
  }
  if (guidance && !guidance.motion_ok) {
    return { ready: false, text: 'The motion gate fails: the aircraft is still moving. Hold it, then capture.' }
  }
  if (!guidance) {
    return {
      ready: true,
      text: `Gates unreported. ${id} will capture ${state.capturePattern} in ${roomId}; the arbiter checks readiness before dispatch.`,
    }
  }
  return { ready: true, text: `All gates pass. ${id} will capture ${state.capturePattern} in ${roomId}.` }
}

export interface FlowStep {
  n: string
  title: string
  done: boolean
  current: boolean
  state: string
  tone: 'ok' | 'warn' | 'danger' | 'muted'
  hint: string
}

export function captureFlow(
  state: ControlState,
  roomId: string,
  roomOk: boolean,
  guidance: CaptureReadiness | null,
): FlowStep[] {
  const one = state.selection.length === 1 ? state.aircraft[state.selection[0]] : undefined
  const oneReady = one !== undefined && isReady(one) && one.device_class === 'aircraft'
  const stepOne =
    one !== undefined && oneReady
      ? `${formatDeviceId(one)} selected, ${motionStateWord(one)} and ready`
      : state.selection.length === 0
      ? 'no aircraft selected'
      : state.selection.length > 1
        ? `${state.selection.length} selected — capture_room takes exactly one`
        : one?.device_class === 'ground_vehicle'
          ? `${formatDeviceId(one)} is a robot — capture_room is not available for robots`
          : `${deviceLabeller(state.aircraft)(state.selection[0])} is not ready`
  const failing = guidance ? failingGates(guidance) : []
  const gatesWord = !guidance
    ? 'gates unreported'
    : failing.length === 0
      ? 'all six gates pass'
      : `gates blocking: ${failing.map((key) => key.replaceAll('_', ' ')).join(', ')}`
  return [
    {
      n: '1',
      title: 'Choose one aircraft',
      done: oneReady,
      current: !oneReady,
      state: stepOne,
      tone: oneReady ? 'ok' : 'warn',
      hint: 'Only ready, selectable aircraft can be chosen. One stays selected once any is.',
    },
    {
      n: '2',
      title: 'Name the room and pattern',
      done: roomOk && oneReady,
      current: oneReady && !roomOk,
      state: roomOk ? `${roomId} · ${state.capturePattern}` : 'room identifier needed',
      tone: roomOk ? 'ok' : 'warn',
      hint: 'The capture id is minted from the intent id at draft time. The pattern decides the coverage you get.',
    },
    {
      n: '3',
      title: 'Review the plan, then confirm',
      done: false,
      current: roomOk && oneReady,
      state: gatesWord,
      tone: !guidance ? 'muted' : failing.length === 0 ? 'ok' : 'danger',
      hint: 'Nothing is sent until the exact Intent v1 envelope is on screen and you confirm it.',
    },
  ]
}

export interface PatternCard {
  id: 'pano_360' | 'reconstruct_8'
  coverage: string
  note: string
}

export const PATTERN_CARDS: readonly PatternCard[] = [
  {
    id: 'pano_360',
    coverage: 'full_equirectangular',
    note: 'One station, complete sphere. Use this unless a mesh is needed.',
  },
  {
    id: 'reconstruct_8',
    coverage: 'incomplete_vertical_coverage',
    note: 'Eight overlapping frames. Ceiling and floor stay thin.',
  },
]

/* Mission tracker */

export interface MissionStep {
  n: string
  gesture: string
  intent: string
  note: string
  status: 'available' | 'unsupported'
}

/** Appendix E, the scripted mission: gesture, canonical intent, and what the relay does with it. */
export const MISSION_STEPS: readonly MissionStep[] = (
  [
    ['Both palms up', 'arm', 'Arm the fleet. No motion yet.'],
    ['Open palm', 'select', 'Select every ready aircraft.'],
    ['Open palm up', 'takeoff', 'Takeoff — risky, so the relay returns a pending object.'],
    ['Thumb up', 'confirm', 'Confirm the pending takeoff. Dwell 400 ms.'],
    ['Diamond', 'formation_set', 'Formation to diamond.'],
    ['Index swipe right, twice', 'translate', 'Translate two steps east.'],
    ['Pinch and raise', 'altitude', 'Altitude up one step.'],
    ['Two fingers held', 'sweep', 'Sweep, then thumb up to confirm, then wait for the lanes.'],
    ['Rock sign', 'come_home', 'Come home to staggered pads.'],
    ['Rock sign, then both palms up', 'land_all', 'Land all, then disarm.'],
  ] as const
).map(([gesture, intent, note], i) => ({
  n: String(i + 1).padStart(2, '0'),
  gesture,
  intent,
  note,
  status:
    intent === 'confirm' || isSupportedIntent(intent as ConsoleIntentName) ? 'available' : 'unsupported',
}))

export const MISSION_PASS_TEXT =
  'Pass — ten steps, zero unsafe commands dispatched, no manual intervention.'
export const MISSION_PASS_RULE =
  'Pass requires all ten steps inside three minutes with zero unsafe commands dispatched.'

/* Requests */

export function requestTone(status: RequestStatus): Tone {
  if (status === 'completed' || status === 'accepted') return 'ok'
  if (status === 'refused' || status === 'failed') return 'danger'
  if (status === 'invalidated' || status === 'pending_confirmation') return 'warn'
  return 'ink'
}

/** Retry is offered on failed and refused requests; the reason it is disabled is stated in text. */
export function retryBlockedReason(request: RequestRecord, state: ControlState): string | null {
  if (request.intent.source === 'language') {
    return 'Disabled: a language plan step cannot be retried outside its exact compiler plan. Compile a fresh plan.'
  }
  const connection =
    request.intent.source === 'keyboard'
      ? state.keyboardConnection
      : request.intent.source === 'webcam'
        ? state.webcamConnection
        : state.connection
  if (!isLinkUp(connection.status)) {
    return `Disabled: the ${request.intent.source} connection is ${connection.status}.`
  }
  const capability = capabilityBlockedReason(state, request.intent.name)
  if (capability) return `Disabled: ${capability}`
  if (followsSelection(request.intent.name)) {
    const gone = request.intent.selection.find((id) => !isReady(state.aircraft[id]))
    if (gone !== undefined) {
      const noun = selectionNoun(state.aircraft, request.intent.selection)
      return `Disabled: ${deviceLabeller(state.aircraft)(gone)} is no longer ready. No substitute ${noun} is selected.`
    }
  }
  return null
}
