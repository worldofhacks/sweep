/**
 * Pure derivations for the Control module, lifted from the Sweep Console v4
 * design's controlSpec, dpad, catalog, slots, captureBlock and flow. Every
 * input is authoritative relay state; nothing here invents a value.
 */
import type { ControlState, RequestRecord, RequestStatus } from '../../control/state'
import { capabilityBlockedReason, formatDroneId } from '../../control/state'
import type { TranslateDirection } from '../../control/intent'
import type {
  ConsoleIntentName,
  DroneId,
  FormationName,
  IntentArgs,
  IntentArgsByName,
  RelayAircraftState,
  SelectionRule,
} from '../../relay/contract'
import { followsSelection, isSupportedAtM20, requiresConfirmation, selectionRule } from '../../relay/contract'
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
  /** Legacy display policy for schema-known names outside the M2.0 implementation set. */
  soft: boolean
  badge: ControlBadge
  /** The one sentence under the control: the blocking reason, the refusal copy, or what a press does. */
  note: string
  noteTone: 'warn' | 'muted'
  rule: SelectionRule
}

export const FORMATION_NAMES: readonly FormationName[] = ['line', 'column', 'circle', 'grid', 'V']

export function connectionReason(state: ControlState): string | null {
  return isLinkUp(state.connection.status)
    ? null
    : `The console connection is ${state.connection.status}. Nothing can be sent.`
}

export const STOP_ACTIVE_REASON =
  'The network stop is active. Motion intents are refused until the relay reports it clear.'
export const NO_SELECTION_REASON = 'No aircraft selected.'
export const NO_READY_REASON = 'No aircraft is ready.'

export function refusalCopy(name: ConsoleIntentName): string {
  return `The relay refuses ${name} as unsupported at M2.0. Press it and the refusal is recorded.`
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
  return `${notReady.map(formatDroneId).join(', ')} ${notReady.length > 1 ? 'are' : 'is'} not ready.`
}

interface GateOptions {
  /** The rule needs at least one selected aircraft. */
  sel?: boolean
  /** Every selected aircraft must be ready. */
  ready?: boolean
  /** Reason that applies before the M2.0 check, for select-all with nothing ready. */
  extra?: string | null
  okNote?: string
}

interface Gate {
  reason: string | null
  soft: boolean
}

/**
 * Reason order: connection, authoritative relay capability, selection rule,
 * then the local M2.0 presentation set and network stop. A valid server profile
 * disables every unimplemented name before the legacy soft-display path.
 */
export function gateControl(state: ControlState, name: ConsoleIntentName, options: GateOptions = {}): Gate {
  const connection = connectionReason(state)
  if (connection) return { reason: connection, soft: false }
  const capability = capabilityBlockedReason(state, name)
  if (capability) return { reason: capability, soft: false }
  if (options.sel && state.selection.length === 0) return { reason: NO_SELECTION_REASON, soft: false }
  if (options.ready) {
    const notReady = notReadySentence(state)
    if (notReady) return { reason: notReady, soft: false }
  }
  if (options.extra) return { reason: options.extra, soft: false }
  if (!isSupportedAtM20(name)) return { reason: refusalCopy(name), soft: true }
  if (state.estop && name !== 'estop' && name !== 'land' && name !== 'land_all') return { reason: STOP_ACTIVE_REASON, soft: false }
  return { reason: null, soft: false }
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
  const supported = isSupportedAtM20(name)
  const gate = gateControl(state, name, options)
  const enabled = gate.reason === null || gate.soft
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
    soft: gate.soft,
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
    control(state, 'disarm', 'Disarm', { name: 'disarm', args: {} }),
    control(
      state,
      'select-all',
      'Select all ready',
      { name: 'select', args: { ids: ready }, targets: ready },
      { extra: ready.length === 0 ? NO_READY_REASON : null, okNote: 'Selects every ready aircraft.' },
    ),
  ]
}

/** Swarm › Motion: every selected aircraft. */
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
    control(state, 'formation_next', 'Formation next', { name: 'formation_next', args: {} }, { sel: true }),
  ]
}

export const MOTION_FOOTNOTE =
  'Greyed labels are disabled by the relay capability profile. Altitude up and down are unsupported too. Steps resolve against the room frame.'

/** Commands: the five formations and the two altitude steps, both unsupported at M2.0. */
export function formationControls(state: ControlState): ControlSpec[] {
  return FORMATION_NAMES.map((name) =>
    control(state, `formation-${name}`, name, { name: 'formation_set', args: { name } }, { sel: true }),
  )
}

export function altitudeControls(state: ControlState): ControlSpec[] {
  return [
    control(state, 'altitude+', 'Altitude up', { name: 'altitude', args: { delta: 1 } }, { sel: true }),
    control(state, 'altitude-', 'Altitude down', { name: 'altitude', args: { delta: -1 } }, { sel: true }),
  ]
}

export type CatalogStatus = 'accepted at M2.0' | 'unsupported' | 'later'

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
    status: spec.supported ? 'accepted at M2.0' : 'unsupported',
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
    (state.estop ? STOP_ACTIVE_REASON : state.selection.length === 0 ? NO_SELECTION_REASON : null)
  )
}

/** Planner slot positions in metres, from the design's slots(name, n, spacing). */
export function formationSlots(name: string, count: number, spacing: number): Array<[number, number]> {
  const n = Math.max(count, 1)
  const mid = (n - 1) / 2
  const out: Array<[number, number]> = []
  for (let i = 0; i < n; i += 1) {
    if (name === 'line') out.push([(i - mid) * spacing, 0])
    else if (name === 'column') out.push([0, (i - mid) * spacing])
    else if (name === 'circle') {
      const r = n > 1 ? spacing / (2 * Math.sin(Math.PI / n)) : 0
      const a = (2 * Math.PI * i) / n - Math.PI / 2
      // Adding zero folds a negative zero from sin or cos into plain zero.
      out.push([r * Math.cos(a) + 0, r * Math.sin(a) + 0])
    } else if (name === 'grid') {
      const c = Math.ceil(Math.sqrt(n))
      out.push([((i % c) - (c - 1) / 2) * spacing, (Math.floor(i / c) - (Math.ceil(n / c) - 1) / 2) * spacing])
    } else out.push([(i - mid) * spacing, Math.abs(i - mid) * spacing * 0.6])
  }
  return out
}

export interface FormationDot {
  id: string
  droneId: DroneId
  left: string
  top: string
  slot: string
  ready: boolean
}

/**
 * Dots for the selected aircraft at their slots. The plot is scale free, so an
 * unreported spacing still places the dots; only the metre labels need it.
 */
export function formationPlot(
  aircraft: RelayAircraftState[],
  name: string | null,
  spacing: number | null,
): FormationDot[] {
  if (name === null || aircraft.length === 0) return []
  const slots = formationSlots(name, aircraft.length, spacing ?? 1)
  const span = Math.max(1.2, ...slots.map(([x, y]) => Math.max(Math.abs(x), Math.abs(y)))) * 2.4
  return aircraft.map((drone, i) => {
    const [x, y] = slots[i]
    return {
      id: formatDroneId(drone.drone_id),
      droneId: drone.drone_id,
      left: `${50 + (x / span) * 100}%`,
      top: `${50 + (y / span) * 100}%`,
      slot:
        spacing === null
          ? `slot ${i + 1} · spacing unreported`
          : `slot ${i + 1} · ${x.toFixed(1)} m, ${y.toFixed(1)} m`,
      ready: isReady(drone),
    }
  })
}

export function formationRelayNote(preview: string | null, reported: string | null): string {
  const shown = preview ?? reported
  if (reported === null) {
    return shown === null
      ? 'The relay has not reported a formation.'
      : `Previewing ${shown}. The relay has not reported a formation — formation_set is refused as unsupported at M2.0.`
  }
  if (shown === reported) return `The relay reports ${reported}.`
  return `Previewing ${shown}. The relay still reports ${reported} — formation_set is refused as unsupported at M2.0.`
}

export interface FanoutRow {
  id: string
  cmd: string
}

/** What the planner would propose per aircraft for a pending, confirmation-gated draft. */
export function fanoutFor(name: ConsoleIntentName, args: IntentArgs, targets: DroneId[]): FanoutRow[] {
  const pattern = 'pattern' in args ? args.pattern : 'the requested pattern'
  return targets.map((droneId, i) => {
    const id = formatDroneId(droneId)
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

export function aircraftChips(state: ControlState): ChipView[] {
  return sortedAircraft(state.aircraft).map((drone) => {
    const selectCapability = capabilityBlockedReason(state, 'select')
    const selectable = selectCapability === null && isReady(drone)
    return {
      droneId: drone.drone_id,
      id: formatDroneId(drone.drone_id),
      sub: `${drone.flight_state ?? 'flight state unreported'} · ${drone.battery === null ? '—' : `${Math.round(drone.battery * 100)}%`}`,
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

/** "D-03 telemetry stale · D-04 control authority missing", or empty when every aircraft is ready. */
export function chipBlockers(state: ControlState): string {
  return sortedAircraft(state.aircraft)
    .filter((drone) => !isReady(drone))
    .map(
      (drone) =>
        `${formatDroneId(drone.drone_id)} ${(drone.readiness_reasons[0] ?? 'not selectable').replaceAll('_', ' ')}`,
    )
    .join(' · ')
}

/* Capture */

export type GuidanceMode = 'visual_advisory' | 'registered_metric'
export type SectorCoverage = 'accepted' | 'weak' | 'unseen'
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
  return `${count('unseen')} unseen, ${count('weak')} weak, ${count('accepted')} accepted`
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
  const id = formatDroneId(state.selection[0])
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
  const oneReady = one !== undefined && isReady(one)
  const stepOne =
    one !== undefined && oneReady
      ? `${formatDroneId(one.drone_id)} selected, ${one.flight_state ?? 'flight state unreported'} and ready`
      : state.selection.length === 0
      ? 'no aircraft selected'
      : state.selection.length > 1
        ? `${state.selection.length} selected — capture_room takes exactly one`
        : `${formatDroneId(state.selection[0])} is not ready`
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
  status: 'accepted at M2.0' | 'unsupported'
}

/** Appendix E, the scripted mission: gesture, canonical intent, and what the relay does with it. */
export const MISSION_STEPS: readonly MissionStep[] = (
  [
    ['Both palms up', 'arm', 'Arm the fleet. No motion yet.'],
    ['Open palm', 'select', 'Select every ready aircraft.'],
    ['Open palm up', 'takeoff', 'Takeoff — risky, so the relay returns a pending object.'],
    ['Thumb up', 'confirm', 'Confirm the pending takeoff. Dwell 400 ms.'],
    ['Circle', 'formation_set', 'Formation to circle. Unsupported at M2.0.'],
    ['Index swipe right, twice', 'translate', 'Translate two steps east.'],
    ['Pinch and raise', 'altitude', 'Altitude up one step. Unsupported at M2.0.'],
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
    intent === 'confirm' || isSupportedAtM20(intent as ConsoleIntentName) ? 'accepted at M2.0' : 'unsupported',
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
  const connection = request.intent.source === 'keyboard' ? state.keyboardConnection : state.connection
  if (!isLinkUp(connection.status)) {
    return `Disabled: the ${request.intent.source} connection is ${connection.status}.`
  }
  const capability = capabilityBlockedReason(state, request.intent.name)
  if (capability) return `Disabled: ${capability}`
  if (followsSelection(request.intent.name)) {
    const gone = request.intent.selection.find((id) => !isReady(state.aircraft[id]))
    if (gone !== undefined) {
      return `Disabled: ${formatDroneId(gone)} is no longer ready. No substitute aircraft is selected.`
    }
  }
  return null
}
