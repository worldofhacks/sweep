import type { ConnectionStatus, ControlState } from '../control/state'
import { formatDroneId } from '../control/state'
import type { DroneId, RelayAircraftState } from '../relay/contract'
import { connectionTone, isLinkUp, sortedAircraft, type Tone } from '../shell/derive'
import { formatAge } from '../shell/format'
import type {
  BundleRef,
  CapturePose,
  CaptureRecord,
  ConfigGroup,
  GenerationJob,
  GenerationJobState,
  NodeRecord,
  RoomCaptureStatus,
  ServiceRecord,
} from './types'

export interface CatalogLink {
  up: boolean
  status: ConnectionStatus
  /** Sentence shown above a module while the console link is not connected. */
  notice: string | null
}

/**
 * Catalog data travels over the console connection. When that link is down the
 * last snapshot stays on screen, marked as such, and nothing can be sent.
 */
export function deriveCatalogLink(
  state: ControlState,
  subject: string,
  blocked: string,
): CatalogLink {
  const status = state.connection.status
  const up = isLinkUp(status)
  if (status === 'connected') return { up, status, notice: null }
  if (status === 'degraded') {
    return { up, status, notice: `The console connection is degraded. ${subject} may lag the relay.` }
  }
  return {
    up,
    status,
    notice: `The console connection is ${status}. ${subject} shows the last snapshot received; ${blocked} until the relay reports connected.`,
  }
}

export function jobTone(state: GenerationJobState): Tone {
  if (state === 'succeeded') return 'ok'
  if (state === 'running' || state === 'queued' || state === 'uploading') return 'warn'
  if (state === 'failed' || state === 'timed_out') return 'danger'
  return 'muted'
}

export const JOB_SENTENCE: Record<GenerationJobState, string> = {
  failed: 'The generation service returned an error. The capture is preserved; retry uses the same bundle.',
  timed_out: 'The job exceeded the service timeout. The capture is preserved; retry uses the same bundle.',
  succeeded: 'The room world is generated and can be opened. Its source photos stay beside it.',
  running: 'The service is generating. The operator can start the next room while this runs.',
  queued: 'Waiting for the service to start the job.',
  uploading: 'Sending the accepted bundle to the World API.',
  draft: 'No bundle has been accepted for this room yet.',
}

export const ROOM_CAPTURE_LABEL: Record<RoomCaptureStatus, string> = {
  captured: 'captured',
  capturing: 'capturing',
  needs_retake: 'needs retake',
  not_captured: 'not captured',
}

export function roomCaptureTone(status: RoomCaptureStatus): Tone {
  if (status === 'captured') return 'ok'
  if (status === 'needs_retake') return 'warn'
  return 'muted'
}

export const MANUAL_PHOTOS_REQUIRED = 3

export function bundleLabel(bundle: BundleRef, manualPhotos: number): string {
  if (bundle.kind === 'manual_phone') return `manual phone fallback · ${manualPhotos} photos`
  return `${bundle.capture_id ?? 'capture unreported'} · ${bundle.kind}`
}

export function bundleImages(bundle: BundleRef, manualPhotos: number): number {
  if (bundle.kind === 'pano_360') return 1
  if (bundle.kind === 'reconstruct_8') return 8
  return manualPhotos
}

export function sameBundle(left: BundleRef | null, right: BundleRef | null): boolean {
  if (left === null || right === null) return left === right
  return left.kind === right.kind && left.capture_id === right.capture_id
}

/** Drone bundles for a room in catalog order, then the manual phone fallback. */
export function bundleOptions(captures: CaptureRecord[], roomId: string): BundleRef[] {
  const drone = captures
    .filter((capture) => capture.room_id === roomId)
    .map((capture) => ({ kind: capture.pattern, capture_id: capture.capture_id }))
  return [...drone, { kind: 'manual_phone', capture_id: null }]
}

/** Design's colour key for the states gallery: one tone per vocabulary value. */
export function vocabTone(value: string): Tone {
  if (['live', 'completed', 'succeeded', 'ready', 'pass', 'connected'].includes(value)) return 'ok'
  if (
    ['offline', 'degraded', 'stale', 'leaving', 'needs retake', 'queued', 'uploading', 'running'].includes(
      value,
    )
  ) {
    return 'warn'
  }
  if (['unreported', 'draft'].includes(value)) return 'muted'
  if (['disconnected', 'failed', 'timed_out', 'refused'].includes(value)) return 'danger'
  return 'ink'
}

/** Design pose line: "x 0.41 y 1.08 z 1.42 · yaw 132.4° · gimbal −12.0° · f 3.2 mm". */
export function formatPose(pose: CapturePose | null): string {
  if (pose === null) return 'pose unreported'
  return `x ${pose.x.toFixed(2)} y ${pose.y.toFixed(2)} z ${pose.z.toFixed(2)} · yaw ${formatSigned(pose.yaw_deg)}° · gimbal ${formatSigned(pose.gimbal_pitch_deg)}° · f ${pose.focal_mm.toFixed(1)} mm`
}

/** Typographic minus, as the design prints it. */
export function formatSigned(value: number): string {
  return value < 0 ? `−${Math.abs(value).toFixed(1)}` : value.toFixed(1)
}

export function filesLabel(files: number): string {
  return `${files} ${files === 1 ? 'file' : 'files'}`
}

export interface CaptureFilter {
  id: string
  label: string
  test: (capture: CaptureRecord) => boolean
}

/** All, then every project (when more than one), room and aircraft seen, then needs retake. */
export function captureFilters(captures: CaptureRecord[]): CaptureFilter[] {
  const projects = unique(captures.map((capture) => capture.project))
  const rooms = unique(captures.map((capture) => capture.room_id))
  const drones = unique(captures.map((capture) => capture.drone_id)).sort((a, b) => a - b)
  return [
    { id: 'all', label: 'All captures', test: () => true },
    ...(projects.length > 1
      ? projects.map((project) => ({
          id: `project:${project}`,
          label: `project ${project}`,
          test: (capture: CaptureRecord) => capture.project === project,
        }))
      : []),
    ...rooms.map((room) => ({
      id: `room:${room}`,
      label: room,
      test: (capture: CaptureRecord) => capture.room_id === room,
    })),
    ...drones.map((id) => ({
      id: `drone:${id}`,
      label: formatDroneId(id),
      test: (capture: CaptureRecord) => capture.drone_id === id,
    })),
    { id: 'retake', label: 'Needs retake', test: (capture) => capture.needs_retake },
  ]
}

export interface CaptureProject {
  project: string
  captures: CaptureRecord[]
}

/** Projects in first-seen order; captures newest first inside each. */
export function groupCapturesByProject(captures: CaptureRecord[]): CaptureProject[] {
  return unique(captures.map((capture) => capture.project)).map((project) => ({
    project,
    captures: captures
      .filter((capture) => capture.project === project)
      .sort((a, b) => b.captured_at - a.captured_at),
  }))
}

function unique<T>(values: T[]): T[] {
  return values.filter((value, index) => values.indexOf(value) === index)
}

export const ACTIVE_JOB_STATES: readonly GenerationJobState[] = ['uploading', 'queued', 'running']

export function canRetryJob(job: GenerationJob): boolean {
  return job.state === 'failed' || job.state === 'timed_out'
}

/** Why a room cannot be submitted right now; null when it can. */
export function submitBlockedReason(
  link: CatalogLink,
  bundle: BundleRef,
  manualPhotos: number,
  job: GenerationJob | undefined,
): string | null {
  if (!link.up) return `The console connection is ${link.status}. Nothing can be submitted.`
  if (bundle.kind === 'manual_phone' && manualPhotos !== MANUAL_PHOTOS_REQUIRED) {
    return `Manual fallback needs exactly ${MANUAL_PHOTOS_REQUIRED} overlapping phone photos; ${manualPhotos} added.`
  }
  if (job && ACTIVE_JOB_STATES.includes(job.state)) {
    return `A job for this room is already ${job.state}. Wait for it to finish or fail.`
  }
  return null
}

export interface NodeCell {
  key: string
  value: string
  tone: Tone
}

/**
 * The design's nine node cells. Relay-owned facts come from the aircraft
 * state; versions, RTT, rate and storage come from the catalog node record
 * and read unreported when the catalog has none.
 */
export function nodeCells(
  drone: RelayAircraftState,
  node: NodeRecord | null,
  now: number,
): NodeCell[] {
  const down = drone.membership === 'disconnected'
  const stale = drone.readiness_reasons.includes('telemetry_stale')
  const rcFirmware = node?.rc_firmware ? `fw ${node.rc_firmware}` : 'fw unreported'
  const bridge =
    node === null
      ? 'unreported'
      : `${node.phone_model ?? 'phone unreported'} · sdk ${node.sdk_release ?? 'unreported'}`
  const lan = node === null ? 'unreported' : node.rtt_ms === null ? 'no route' : `${node.rtt_ms} ms`
  const rate =
    node === null || node.telemetry_rate_hz === null
      ? 'unreported'
      : `${node.telemetry_rate_hz.toFixed(1)} Hz`
  const video = drone.video
  const videoValue =
    video === undefined
      ? 'unreported'
      : `${video.status}${video.last_frame_at !== null ? ` · ${formatAge(now - video.last_frame_at)}` : ''}`
  const storage = down
    ? 'unknown'
    : node === null || node.storage_free_gb === null
      ? 'unreported'
      : `${node.storage_free_gb} GB free`
  const patterns = drone.camera_patterns.length
  return [
    {
      key: 'RC controller',
      value: `${drone.control_authority ? 'standby' : 'in control'} · ${rcFirmware}`,
      tone: drone.control_authority ? 'ink' : 'danger',
    },
    {
      key: 'Android bridge',
      value: down ? 'down' : bridge,
      tone: down ? 'danger' : node === null ? 'muted' : 'ink',
    },
    {
      key: 'LAN',
      value: lan,
      tone: node === null ? 'muted' : node.rtt_ms === null ? 'danger' : node.rtt_ms > 60 ? 'warn' : 'ink',
    },
    { key: 'Relay', value: down ? 'disconnected' : 'connected', tone: down ? 'danger' : 'ok' },
    {
      key: 'Telemetry',
      value: stale
        ? `stale ${drone.last_seen_at === null ? 'unreported' : formatAge(now - drone.last_seen_at)}`
        : rate,
      tone: stale ? 'warn' : rate === 'unreported' ? 'muted' : 'ink',
    },
    {
      key: 'Camera',
      value: patterns ? `ready · ${patterns} ${patterns === 1 ? 'pattern' : 'patterns'}` : 'not ready',
      tone: patterns ? 'ink' : 'danger',
    },
    { key: 'Video', value: videoValue, tone: video === undefined ? 'muted' : vocabTone(video.status) },
    { key: 'Storage', value: storage, tone: storage === 'unreported' ? 'muted' : 'ink' },
    {
      key: 'Firmware',
      value: node?.aircraft_firmware ? `aircraft ${node.aircraft_firmware}` : 'unreported',
      tone: node?.aircraft_firmware ? 'ink' : 'muted',
    },
  ]
}

/** The design's per-node error line: what is wrong and what to do. */
export function nodeError(drone: RelayAircraftState): string | null {
  if (drone.membership === 'disconnected') {
    return 'Adapter connection lost. Power-cycle the bridge phone, then rejoin; the aircraft returns with a higher epoch.'
  }
  if (drone.readiness_reasons.includes('telemetry_stale')) {
    return "Telemetry stopped. Check the bridge phone's LAN link before commanding motion."
  }
  if (!drone.control_authority) {
    return 'The RC pilot holds authority. Sweep commands are refused until authority returns.'
  }
  return null
}

export function nodeRecordFor(
  nodes: Record<DroneId, NodeRecord> | null,
  droneId: DroneId,
): NodeRecord | null {
  return nodes?.[droneId] ?? null
}

/** Relay and keyboard stop rows come from live connection state, never the catalog. */
export function liveServices(state: ControlState): ServiceRecord[] {
  const console = state.connection
  const keyboard = state.keyboardConnection
  const consoleNote =
    console.status === 'connected'
      ? keyboard.status === 'connected'
        ? 'Two sockets authenticated: console and keyboard.'
        : `Console socket authenticated; the keyboard socket is ${keyboard.status}.`
      : (console.reason ?? `The console socket is ${console.status}.`)
  return [
    {
      service_id: 'relay',
      label: 'Relay',
      status: console.status,
      tone: serviceTone(console.status),
      note: consoleNote,
    },
    {
      service_id: 'keyboard_stop',
      label: 'Keyboard stop',
      status: keyboard.status,
      tone: serviceTone(keyboard.status),
      note:
        keyboard.status === 'connected'
          ? 'Carries Shift+Escape only.'
          : (keyboard.reason ?? 'Carries Shift+Escape only; unavailable until it reconnects.'),
    },
  ]
}

function serviceTone(status: ConnectionStatus): ServiceRecord['tone'] {
  const tone = connectionTone(status)
  return tone === 'ok' || tone === 'warn' ? tone : 'danger'
}

export const LADDER: readonly string[] = [
  'full',
  'no video',
  'no language',
  'webcam only',
  'keyboard stop only',
]

export interface LadderRung {
  label: string
  current: boolean
}

/**
 * The rung the console can prove from live state: video liveness and the two
 * sockets. Language and webcam report nothing yet, so those rungs are never
 * marked current.
 */
export function ladderRungs(state: ControlState): LadderRung[] {
  const consoleUp = isLinkUp(state.connection.status)
  const keyboardUp = isLinkUp(state.keyboardConnection.status)
  const anyVideo = sortedAircraft(state.aircraft).some((drone) => drone.video?.status === 'live')
  const current = consoleUp
    ? anyVideo
      ? 'full'
      : 'no video'
    : keyboardUp
      ? 'keyboard stop only'
      : null
  return LADDER.map((label) => ({ label, current: label === current }))
}

export function ladderSentence(state: ControlState, rungs: LadderRung[]): string {
  const current = rungs.find((rung) => rung.current)
  if (current) return `Current rung: ${current.label}.`
  return `Both sockets are ${state.connection.status}; no rung is held. The physical RC remains primary.`
}

export interface ConfigSemantics {
  word: string
  sentence: string
  action: string
  tone: Tone
}

export function configSemantics(group: ConfigGroup): ConfigSemantics {
  return group.staged
    ? {
        word: 'pending until the next run',
        sentence: 'Safety-sensitive. Staged and applied between runs.',
        action: 'Stage for the next run',
        tone: 'warn',
      }
    : { word: 'live', sentence: 'Applies now.', action: 'Save', tone: 'ok' }
}

export function configurationChangedDetail(title: string): string {
  return `Configuration changed: ${title}. Build and confirm a new preview.`
}

export interface VocabDomain {
  domain: string
  values: string[]
}

/** The design's thirteen vocabulary domains, in order. */
export const VOCAB: readonly VocabDomain[] = [
  { domain: 'Connection', values: ['connecting', 'connected', 'degraded', 'disconnected'] },
  { domain: 'Membership', values: ['registered', 'ready', 'leaving', 'disconnected', 'degraded'] },
  {
    domain: 'Membership events',
    values: [
      'join',
      'readiness',
      'graceful_leave',
      'graceful_leave_completed',
      'unexpected_loss',
      'telemetry_stale',
      'telemetry_recovered',
    ],
  },
  {
    domain: 'Flight state',
    values: ['disarmed', 'landed', 'armed', 'taking_off', 'airborne', 'hovering', 'landing', 'emergency'],
  },
  {
    domain: 'Intent lifecycle',
    values: [
      'draft',
      'pending_confirmation',
      'sent',
      'accepted',
      'refused',
      'executing',
      'completed',
      'failed',
      'invalidated',
      'cancelled',
    ],
  },
  { domain: 'Stream status', values: ['live', 'offline', 'unreported'] },
  { domain: 'Capture pattern', values: ['pano_360', 'reconstruct_8'] },
  { domain: 'Coverage', values: ['full_equirectangular', 'incomplete_vertical_coverage'] },
  {
    domain: 'Capture progress',
    values: ['ready', 'capturing', 'downloading', 'needs_retake', 'disconnected'],
  },
  { domain: 'Guidance mode', values: ['visual_advisory', 'registered_metric'] },
  {
    domain: 'Generation job',
    values: ['draft', 'uploading', 'queued', 'running', 'succeeded', 'failed', 'timed_out'],
  },
  { domain: 'Mode', values: ['indoor', 'outdoorC', 'outdoorF'] },
  {
    domain: 'Provenance',
    values: [
      'adapter_signature',
      'relay_transport_attestation',
      'relay_freshness_attestation',
      'authenticated_adapter_telemetry',
    ],
  },
]

export function vocabNote(value: string): string {
  return value === 'outdoorC' || value === 'outdoorF' ? 'unsupported' : ''
}
