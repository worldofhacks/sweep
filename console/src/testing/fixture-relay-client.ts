import type { CatalogClient, CatalogListener } from '../catalog/client'
import type {
  BundleRef,
  CatalogSnapshot,
  ConfigSnapshot,
  GenerationJob,
  GenerationJobState,
  NodeRecord,
} from '../catalog/types'
import type { RelayClient, RelayClientEvent, RelayClientListener } from '../relay/client'
import type {
  DroneId,
  IntentV1,
  RelayAircraftState,
  RelayServerEvent,
  IntentSource,
} from '../relay/contract'
import { C1_BASIC_CONTROL_INTENTS, isSupportedIntent } from '../relay/contract'

export type FixtureFleetSize = 4 | 6

/**
 * Named fixture scenarios. `control` is the original four- or six-drone
 * contract fixture; `pending4`, `six6`, and `down` are the Sweep Console v4
 * design scenarios as relay data only. Nothing here reaches production state.
 */
export type FixtureScenarioName = 'control' | 'pending4' | 'six6' | 'down'

export const FIXTURE_SCENARIO_NAMES: readonly FixtureScenarioName[] = [
  'control',
  'pending4',
  'six6',
  'down',
]

export function isFixtureScenarioName(value: string): value is FixtureScenarioName {
  return (FIXTURE_SCENARIO_NAMES as readonly string[]).includes(value)
}

type FixtureLinkStatus = 'connected' | 'degraded' | 'disconnected'

interface FixtureLink {
  status: FixtureLinkStatus
  reason: string
}

interface FixtureDeparture {
  droneId: DroneId
  connectionEpoch: number
  rosterVersion: number
  ageMs: number
  reason: string
  adapterId: string
}

export interface FixtureScenario {
  name: FixtureScenarioName
  rosterVersion: number
  formation: string
  spacing: number
  console: FixtureLink
  keyboard: FixtureLink
  fleet: (now: number) => RelayAircraftState[]
  /** Membership events emitted before the first state frame. */
  departures: FixtureDeparture[]
  /** Relay-side pending record; opaque to the console, which discards it. */
  pending: Record<string, unknown> | null
  /** Captures, building, jobs, node details, services, metrics, configuration. */
  catalog: (now: number) => CatalogSnapshot
}

const CONNECTED: FixtureLink = {
  status: 'connected',
  reason: 'DEVELOPMENT FIXTURE — no commands leave this browser.',
}

export class FixtureRelayClient implements RelayClient {
  readonly transport = 'fixture' as const
  readonly sent: IntentV1[] = []
  private readonly listeners = new Set<RelayClientListener>()
  private readonly scenario: FixtureScenario
  private selection: DroneId[] = [1]
  private sequence = 0
  private readonly sessionId: string
  private readonly now: () => number
  private readonly source: IntentSource
  private armed: boolean

  constructor(
    sessionId: string,
    now: () => number = () => Date.now(),
    source: IntentSource = 'console',
    scenario: FixtureFleetSize | FixtureScenarioName | boolean = 4,
    armed = true,
  ) {
    this.sessionId = sessionId
    this.now = now
    this.source = source
    this.armed = typeof scenario === 'boolean' ? scenario : armed
    this.scenario = typeof scenario === 'boolean' ? controlScenario(4) : typeof scenario === 'number' ? controlScenario(scenario) : fixtureScenario(scenario)
  }

  private get link(): FixtureLink {
    return this.source === 'keyboard' ? this.scenario.keyboard : this.scenario.console
  }

  start(): void {
    const link = this.link
    this.emitConnection(link.status, link.reason)
    if (link.status === 'disconnected') return
    this.emitServer({
      v: 1,
      t: this.now(),
      event_id: this.nextEventId(),
      type: 'auth.accepted',
      session: this.sessionId,
      source: this.source,
      drone_id: null,
    })
    for (const departure of this.scenario.departures) {
      this.emitServer({
        v: 1,
        t: this.now() - departure.ageMs,
        event_id: this.nextEventId(),
        type: 'membership',
        session: this.sessionId,
        roster_version: departure.rosterVersion,
        action: 'unexpected_loss',
        drone_id: departure.droneId,
        connection_epoch: departure.connectionEpoch,
        membership: 'disconnected',
        readiness_reasons: ['disconnected'],
        adapter_id: departure.adapterId,
        capabilities: ['flight'],
        provenance: 'relay_transport_attestation',
        reason: departure.reason,
      })
    }
    this.emitState(this.now())
  }

  stop(): void {}

  subscribe(listener: RelayClientListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    if (this.link.status === 'disconnected') {
      throw new Error('Fixture relay is disconnected; the intent was not sent.')
    }
    const t = this.now()
    if (!isSupportedIntent(intent.name)) {
      // The same refusal relay/intent_v1.py returns for a name outside the advertised profile.
      this.emitServer({
        v: 1,
        t,
        event_id: this.nextEventId(),
        type: 'refusal',
        session: this.sessionId,
        intent_id: intent.intent_id,
        command_id: null,
        status: 'refused',
        source: 'relay',
        reason: 'unsupported',
        detail: `${intent.name} is outside the M2.0 capability set`,
        roster_version: this.scenario.rosterVersion,
        drone_id: null,
        connection_epoch: null,
      })
      return
    }
    if (intent.name === 'select' && 'ids' in intent.args) {
      this.selection = [...intent.args.ids]
    } else if (intent.name === 'arm') {
      this.armed = true
    }
    if (intent.name === 'select' || intent.name === 'arm') {
      this.emitState(t)
    }
    this.emitServer({
      v: 1,
      t,
      event_id: this.nextEventId(),
      type: 'acknowledgement',
      session: this.sessionId,
      intent_id: intent.intent_id,
      command_id: null,
      status: 'accepted',
      source: 'relay',
      drone_id: null,
      connection_epoch: null,
      reason: null,
      detail: 'Accepted by the explicit development fixture.',
      roster_version: this.scenario.rosterVersion,
    })
  }

  emitServer(event: RelayServerEvent): void {
    this.emit({ kind: 'server_event', event })
  }

  emitConnection(status: FixtureLinkStatus, reason?: string): void {
    this.emit({
      kind: 'connection',
      connection: {
        status,
        transport: this.transport,
        changedAt: this.now(),
        reason,
      },
    })
  }

  private emitState(t: number): void {
    this.emitServer({
      v: 1,
      t,
      event_id: this.nextEventId(),
      type: 'state',
      session: this.sessionId,
      roster_version: this.scenario.rosterVersion,
      armed: this.armed,
      estop: false,
      selection: this.selection,
      formation: this.scenario.formation,
      spacing: this.scenario.spacing,
      mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: this.scenario.pending,
      accepted_plan: null,
      drones: this.scenario.fleet(this.now()),
    })
  }

  private nextEventId(): string {
    this.sequence += 1
    return `fixture-${this.source}-event-${this.sequence}`
  }

  private emit(event: RelayClientEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }
}

function controlScenario(fleetSize: FixtureFleetSize): FixtureScenario {
  return {
    name: 'control',
    rosterVersion: 7,
    formation: 'none',
    spacing: 0.8,
    console: CONNECTED,
    keyboard: CONNECTED,
    fleet: (now) => fixtureAircraft(now, fleetSize),
    departures: [],
    pending: null,
    catalog: emptyCatalog,
  }
}

/** The Sweep Console v4 design scenarios, expressed as relay data. */
export function fixtureScenario(name: FixtureScenarioName): FixtureScenario {
  switch (name) {
    case 'control':
      return controlScenario(4)
    case 'pending4':
      return {
        name,
        rosterVersion: 9,
        formation: 'line',
        spacing: 1.5,
        console: CONNECTED,
        keyboard: CONNECTED,
        fleet: designFourFleet,
        departures: [departedFive(8)],
        pending: {
          intent_id: 'fixture-pending-cap-0147',
          name: 'capture_room',
          args: { room_id: 'kitchen-01', capture_id: 'cap-0147', pattern: 'pano_360' },
          targets: [1],
          roster_version: 9,
          source: 'console',
        },
        catalog: (now) => designCatalog(now, 4),
      }
    case 'six6':
      return {
        name,
        rosterVersion: 12,
        formation: 'line',
        spacing: 1.5,
        console: {
          status: 'degraded',
          reason: 'A frame arrived that could not be parsed and was dropped.',
        },
        keyboard: CONNECTED,
        fleet: designSixFleet,
        departures: [departedFive(11)],
        pending: null,
        catalog: (now) => designCatalog(now, 6),
      }
    case 'down': {
      const reason =
        'Relay disconnected. Reload the console from the operator shell to reconnect. Physical RC remains primary.'
      return {
        name,
        rosterVersion: 9,
        formation: 'line',
        spacing: 1.5,
        console: { status: 'disconnected', reason },
        keyboard: { status: 'disconnected', reason },
        fleet: designFourFleet,
        departures: [],
        pending: null,
        catalog: (now) => designCatalog(now, 4),
      }
    }
  }
}

export type Scheduler = (callback: () => void, delayMs: number) => () => void

const timeoutScheduler: Scheduler = (callback, delayMs) => {
  const id = setTimeout(callback, delayMs)
  return () => clearTimeout(id)
}

/** A scheduler tests drive by hand so the job chain is deterministic. */
export function manualScheduler() {
  const queue: Array<{ at: number; run: () => void; done: boolean }> = []
  let elapsed = 0
  const schedule: Scheduler = (callback, delayMs) => {
    const entry = { at: elapsed + delayMs, run: callback, done: false }
    queue.push(entry)
    return () => {
      entry.done = true
    }
  }
  return {
    schedule,
    advance(ms: number): void {
      elapsed += ms
      queue
        .filter((entry) => !entry.done && entry.at <= elapsed)
        .sort((a, b) => a.at - b.at)
        .forEach((entry) => {
          entry.done = true
          entry.run()
        })
    },
    pending(): number {
      return queue.filter((entry) => !entry.done).length
    },
  }
}

/** The design's job chain after a submit or retry. */
export const FIXTURE_JOB_CHAIN: ReadonlyArray<{ state: GenerationJobState; afterMs: number }> = [
  { state: 'queued', afterMs: 1_400 },
  { state: 'running', afterMs: 2_800 },
  { state: 'succeeded', afterMs: 6_400 },
]

const FIXTURE_DISCONNECTED = 'Fixture relay is disconnected; nothing was sent.'

/**
 * The design's catalog fixtures as a catalog client. Submits and retries run
 * the job chain on the injected scheduler; downloads, exports and
 * configuration changes answer with the design's sentences.
 */
export class FixtureCatalogClient implements CatalogClient {
  private readonly listeners = new Set<CatalogListener>()
  private readonly scenario: FixtureScenario
  private readonly now: () => number
  private readonly schedule: Scheduler
  private readonly cancels: Array<() => void> = []
  private snapshot: CatalogSnapshot
  private sequence = 0

  constructor(
    scenario: FixtureFleetSize | FixtureScenarioName | boolean = 4,
    now: () => number = () => Date.now(),
    schedule: Scheduler = timeoutScheduler,
  ) {
    this.scenario = typeof scenario === 'boolean' ? controlScenario(4) : typeof scenario === 'number' ? controlScenario(scenario) : fixtureScenario(scenario)
    this.now = now
    this.schedule = schedule
    this.snapshot = this.scenario.catalog(now())
  }

  get current(): CatalogSnapshot {
    return this.snapshot
  }

  subscribe(listener: CatalogListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  start(): void {
    this.emit()
  }

  stop(): void {
    this.cancels.splice(0).forEach((cancel) => cancel())
  }

  async submitGeneration(roomId: string, bundle: BundleRef): Promise<void> {
    this.requireLink()
    this.startJob(roomId, bundle)
  }

  async retryGeneration(roomId: string): Promise<void> {
    this.requireLink()
    const job = this.snapshot.jobs?.find((item) => item.room_id === roomId)
    if (!job || (job.state !== 'failed' && job.state !== 'timed_out')) {
      throw new Error(`No failed or timed-out job exists for ${roomId}; nothing was submitted.`)
    }
    if (!job.bundle) {
      throw new Error(`No bundle is recorded for ${roomId}; nothing was submitted.`)
    }
    this.startJob(roomId, job.bundle)
  }

  async addManualPhotos(roomId: string, count: number): Promise<void> {
    this.requireLink()
    const building = this.snapshot.building
    if (!building || !building.rooms.some((room) => room.room_id === roomId)) {
      throw new Error(`No room ${roomId} is catalogued; no photos were added.`)
    }
    this.snapshot = {
      ...this.snapshot,
      building: {
        ...building,
        rooms: building.rooms.map((room) =>
          room.room_id === roomId
            ? { ...room, manual_photos: room.manual_photos + Math.max(0, Math.floor(count)) }
            : room,
        ),
      },
    }
    this.emit()
  }

  async stageCaptureSet(captureId: string): Promise<string> {
    this.requireLink()
    const capture = this.snapshot.captures?.find((item) => item.capture_id === captureId)
    if (!capture) throw new Error(`No capture ${captureId} is catalogued; nothing was staged.`)
    const files = `${capture.files} ${capture.files === 1 ? 'file' : 'files'}`
    return `${capture.capture_id} — ${files} staged to session/captures, checksum ${capture.checksum ?? 'unreported'} verified against the aircraft's manifest.`
  }

  async exportCaptureMetadata(captureId: string): Promise<string> {
    this.requireLink()
    const capture = this.snapshot.captures?.find((item) => item.capture_id === captureId)
    if (!capture) throw new Error(`No capture ${captureId} is catalogued; nothing was exported.`)
    return `${capture.capture_id}.json exported: pattern, coverage label, pose, camera intrinsics, quality results and checksums. Media files are not re-encoded.`
  }

  async applyConfig(groupId: string, values: Record<string, string>): Promise<void> {
    this.requireLink()
    const config = this.requireGroup(groupId)
    this.snapshot = {
      ...this.snapshot,
      config: {
        ...config,
        groups: config.groups.map((group) =>
          group.group_id === groupId
            ? {
                ...group,
                fields: group.fields.map((field) =>
                  field.key in values ? { ...field, value: values[field.key] } : field,
                ),
              }
            : group,
        ),
      },
    }
    this.emit()
  }

  async stageConfig(groupId: string, values: Record<string, string>): Promise<void> {
    this.requireLink()
    const config = this.requireGroup(groupId)
    const untouched = config.staged_changes.filter(
      (change) => change.group_id !== groupId || !(change.key in values),
    )
    const staged = Object.entries(values).map(([key, value]) => ({ group_id: groupId, key, value }))
    this.snapshot = {
      ...this.snapshot,
      config: { ...config, staged_changes: [...untouched, ...staged] },
    }
    this.emit()
  }

  private requireLink(): void {
    if (this.scenario.console.status === 'disconnected') throw new Error(FIXTURE_DISCONNECTED)
  }

  private requireGroup(groupId: string): ConfigSnapshot {
    const config = this.snapshot.config
    if (!config || !config.groups.some((group) => group.group_id === groupId)) {
      throw new Error(`No configuration group ${groupId} is reported; nothing was changed.`)
    }
    return config
  }

  private startJob(roomId: string, bundle: BundleRef): void {
    this.sequence += 1
    const operationId = `op_${(0x8f31c2 + this.sequence * 0x1d3).toString(16)}`
    const worldId = `wld_${7742 + this.sequence * 61}`
    this.setJob(roomId, {
      room_id: roomId,
      state: 'uploading',
      operation_id: operationId,
      world_id: null,
      model: 'world-gen-1',
      updated_at: this.now(),
      assets: `${bundleAssets(bundle)}, 0 of ${bundleImages(bundle)} sent`,
      public: false,
      bundle,
    })
    for (const step of FIXTURE_JOB_CHAIN) {
      this.cancels.push(
        this.schedule(() => {
          const job = this.snapshot.jobs?.find((item) => item.room_id === roomId)
          if (!job || job.operation_id !== operationId) return
          this.setJob(roomId, {
            ...job,
            state: step.state,
            updated_at: this.now(),
            world_id: step.state === 'succeeded' ? worldId : job.world_id,
            assets:
              step.state === 'succeeded'
                ? `${bundleAssets(bundle)}, 1 mesh, 1 preview`
                : `${bundleAssets(bundle)} uploaded`,
          })
        }, step.afterMs),
      )
    }
  }

  private setJob(roomId: string, job: GenerationJob): void {
    const jobs = this.snapshot.jobs ?? []
    const exists = jobs.some((item) => item.room_id === roomId)
    this.snapshot = {
      ...this.snapshot,
      jobs: exists ? jobs.map((item) => (item.room_id === roomId ? job : item)) : [...jobs, job],
    }
    this.emit()
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener(this.snapshot))
  }
}

function bundleImages(bundle: BundleRef): number {
  if (bundle.kind === 'pano_360') return 1
  if (bundle.kind === 'reconstruct_8') return 8
  return 3
}

function bundleAssets(bundle: BundleRef): string {
  if (bundle.kind === 'pano_360') return '1 pano'
  if (bundle.kind === 'reconstruct_8') return '8 frames'
  return '3 phone photos'
}

/** The `control` scenario reports every catalog surface as present but empty. */
export function emptyCatalog(): CatalogSnapshot {
  return {
    captures: [],
    building: { building_id: 'bld-01', label: 'ground floor', floor_plan: null, rooms: [] },
    jobs: [],
    nodes: {},
    services: [],
    metrics: [],
    config: { groups: [], staged_changes: [], modes: [] },
  }
}

/** The Sweep Console v4 design's catalog tables. */
export function designCatalog(now: number, fleetSize: FixtureFleetSize): CatalogSnapshot {
  const nodes: Record<DroneId, NodeRecord> = {}
  for (let id = 1; id <= fleetSize; id += 1) {
    const down = fleetSize === 6 && id === 5
    nodes[id] = {
      drone_id: id,
      rc_firmware: '2.4.1',
      aircraft_firmware: '0.9.7',
      phone_model: 'Pixel 7a',
      sdk_release: '1.3.0',
      rtt_ms: down ? null : 18,
      telemetry_rate_hz: down ? 0 : 29.4,
      storage_free_gb: 12 + id * 3,
    }
  }
  return {
    captures: [
      {
        capture_id: 'cap-0147',
        project: 'ground-floor',
        room_id: 'kitchen-01',
        drone_id: 1,
        pattern: 'pano_360',
        coverage: 'full_equirectangular',
        files: 1,
        captured_at: now - 402_000,
        quality: 'pass',
        needs_retake: false,
        checksum: 'sha256:9f2c41ab…7d10',
        pose: { x: 0.41, y: 1.08, z: 1.42, yaw_deg: 132.4, gimbal_pitch_deg: -12, focal_mm: 3.2 },
      },
      {
        capture_id: 'cap-0146',
        project: 'ground-floor',
        room_id: 'kitchen-01',
        drone_id: 1,
        pattern: 'reconstruct_8',
        coverage: 'incomplete_vertical_coverage',
        files: 8,
        captured_at: now - 902_000,
        quality: 'fail',
        needs_retake: true,
        checksum: 'sha256:1ba07c39…44e2',
        pose: { x: 0.38, y: 1.11, z: 1.4, yaw_deg: 44.9, gimbal_pitch_deg: -10.5, focal_mm: 3.2 },
      },
      {
        capture_id: 'cap-0142',
        project: 'ground-floor',
        room_id: 'hall-02',
        drone_id: 2,
        pattern: 'pano_360',
        coverage: 'full_equirectangular',
        files: 1,
        captured_at: now - 3_600_000,
        quality: 'pass',
        needs_retake: false,
        checksum: 'sha256:c47e9012…9a55',
        pose: { x: 2.9, y: 0.44, z: 1.45, yaw_deg: 271, gimbal_pitch_deg: -8, focal_mm: 3.2 },
      },
      {
        capture_id: 'cap-0139',
        project: 'ground-floor',
        room_id: 'studio-03',
        drone_id: 2,
        pattern: 'reconstruct_8',
        coverage: 'incomplete_vertical_coverage',
        files: 8,
        captured_at: now - 7_200_000,
        quality: 'pass',
        needs_retake: false,
        checksum: 'sha256:70d1e5f8…2c31',
        pose: { x: 4.12, y: 2.02, z: 1.38, yaw_deg: 12.6, gimbal_pitch_deg: -14, focal_mm: 3.2 },
      },
    ],
    building: {
      building_id: 'bld-01',
      label: 'ground floor',
      floor_plan: 'floorplan-gf.svg',
      rooms: [
        {
          room_id: 'kitchen-01',
          capture_status: 'captured',
          doorways: ['hall-02'],
          accepted_bundle: { kind: 'pano_360', capture_id: 'cap-0147' },
          manual_photos: 0,
          model: 'world-gen-1',
        },
        {
          room_id: 'hall-02',
          capture_status: 'capturing',
          doorways: ['kitchen-01', 'studio-03', 'stair-04'],
          accepted_bundle: { kind: 'pano_360', capture_id: 'cap-0142' },
          manual_photos: 0,
          model: 'world-gen-1',
        },
        {
          room_id: 'studio-03',
          capture_status: 'needs_retake',
          doorways: ['hall-02'],
          accepted_bundle: { kind: 'reconstruct_8', capture_id: 'cap-0139' },
          manual_photos: 0,
          model: 'world-gen-1',
        },
        {
          room_id: 'stair-04',
          capture_status: 'not_captured',
          doorways: ['hall-02', 'lobby-06'],
          accepted_bundle: { kind: 'manual_phone', capture_id: null },
          manual_photos: 3,
          model: 'world-gen-1',
        },
      ],
    },
    jobs: [
      {
        room_id: 'kitchen-01',
        state: 'succeeded',
        operation_id: 'op_8f31c2',
        world_id: 'wld_7742',
        model: 'world-gen-1',
        updated_at: now - 380_000,
        assets: '1 pano, 1 mesh, 1 preview',
        public: false,
        bundle: { kind: 'pano_360', capture_id: 'cap-0147' },
      },
      {
        room_id: 'hall-02',
        state: 'running',
        operation_id: 'op_9a20de',
        world_id: null,
        model: 'world-gen-1',
        updated_at: now - 64_000,
        assets: '8 frames uploaded',
        public: false,
        bundle: { kind: 'pano_360', capture_id: 'cap-0142' },
      },
      {
        room_id: 'studio-03',
        state: 'failed',
        operation_id: 'op_2c8811',
        world_id: null,
        model: 'world-gen-1',
        updated_at: now - 1_200_000,
        assets: '8 frames uploaded',
        public: false,
        bundle: { kind: 'reconstruct_8', capture_id: 'cap-0139' },
      },
      {
        room_id: 'stair-04',
        state: 'timed_out',
        operation_id: 'op_5510aa',
        world_id: null,
        model: 'world-gen-1',
        updated_at: now - 2_400_000,
        assets: '1 pano uploaded',
        public: false,
        bundle: { kind: 'pano_360', capture_id: null },
      },
      {
        room_id: 'store-05',
        state: 'queued',
        operation_id: 'op_77b304',
        world_id: null,
        model: 'world-gen-1',
        updated_at: now - 20_000,
        assets: '3 phone photos',
        public: false,
        bundle: { kind: 'manual_phone', capture_id: null },
      },
      {
        room_id: 'lobby-06',
        state: 'uploading',
        operation_id: 'op_11ffa0',
        world_id: null,
        model: 'world-gen-1',
        updated_at: now - 8_000,
        assets: '8 frames, 3 of 8 sent',
        public: false,
        bundle: { kind: 'reconstruct_8', capture_id: null },
      },
      {
        room_id: 'corridor-07',
        state: 'draft',
        operation_id: null,
        world_id: null,
        model: 'world-gen-1',
        updated_at: now,
        assets: 'no bundle accepted yet',
        public: false,
        bundle: null,
      },
    ],
    nodes,
    services: [
      {
        service_id: 'media_server',
        label: 'Media server',
        status: `${fleetSize} streams named drone1…drone${fleetSize}`,
        tone: 'ok',
        note: 'Stream names are derived; adapter URLs are never rendered.',
      },
      {
        service_id: 'world_api',
        label: 'World API',
        status: 'reachable · world-gen-1',
        tone: 'ok',
        note: 'All submissions carry public false.',
      },
      {
        service_id: 'storage',
        label: 'Storage',
        status: '412 GB free',
        tone: 'ok',
        note: 'Captures land under the session id.',
      },
    ],
    metrics: [
      { key: 'unsafe commands dispatched', value: '0', note: '0 required', tone: 'ok' },
      { key: 'intent latency p50', value: '41 ms', note: '', tone: 'ink' },
      { key: 'intent latency p95', value: '84 ms', note: 'under 300 ms to first motion', tone: 'ink' },
      { key: 'gesture to intent', value: '118 ms', note: 'under 150 ms', tone: 'ok' },
      { key: 'telemetry rate', value: '29.4 Hz', note: '10 to 50 Hz', tone: 'ink' },
      { key: 'video glass-to-glass', value: '210 ms', note: 'under 300 ms on WebRTC', tone: 'ok' },
      { key: 'detection to alert', value: '640 ms', note: 'under 1 s', tone: 'ok' },
      { key: 'refusals this session', value: '6', note: 'by rule, in the ledger', tone: 'warn' },
      { key: 'gesture false positives', value: '0.4 / 5 min', note: 'under 1 per 5 min', tone: 'ok' },
    ],
    config: {
      groups: [
        {
          group_id: 'input_device',
          title: 'Input device',
          staged: false,
          fields: [
            { key: 'gesture_camera', label: 'Gesture camera', value: 'FaceTime HD (index 0)' },
            { key: 'tracking', label: 'Tracking', value: 'disabled' },
            { key: 'microphone', label: 'Microphone', value: 'MacBook Pro mic' },
          ],
        },
        {
          group_id: 'camera',
          title: 'Camera',
          staged: false,
          fields: [
            { key: 'gimbal_pitch_default', label: 'Gimbal pitch default', value: '−12°' },
            { key: 'exposure', label: 'Exposure', value: 'auto' },
            { key: 'frame_format', label: 'Frame format', value: 'jpeg' },
          ],
        },
        {
          group_id: 'capture_pattern_defaults',
          title: 'Capture pattern defaults',
          staged: false,
          fields: [
            { key: 'pattern', label: 'Pattern', value: 'pano_360' },
            { key: 'overlap', label: 'Overlap', value: '35%' },
            { key: 'dwell', label: 'Dwell', value: '1.2 s' },
          ],
        },
        {
          group_id: 'world_api',
          title: 'World API',
          staged: false,
          fields: [
            { key: 'endpoint', label: 'Endpoint', value: 'world-gen-1' },
            { key: 'visibility', label: 'Visibility', value: 'public false (fixed)' },
            { key: 'retry_limit', label: 'Retry limit', value: '3' },
          ],
        },
        {
          group_id: 'media',
          title: 'Media',
          staged: false,
          fields: [
            { key: 'stream_naming', label: 'Stream naming', value: 'drone{id}' },
            { key: 'download_path', label: 'Download path', value: 'session/captures' },
          ],
        },
        {
          group_id: 'thresholds',
          title: 'Thresholds',
          staged: true,
          fields: [
            { key: 'battery_reserve', label: 'Battery reserve', value: '28%' },
            { key: 'ceiling', label: 'Ceiling', value: '2.4 m' },
            { key: 'spacing_minimum', label: 'Spacing minimum', value: '1.2 m' },
            { key: 'link_minimum', label: 'Link minimum', value: '45%' },
          ],
        },
        {
          group_id: 'connection',
          title: 'Connection',
          staged: true,
          fields: [
            { key: 'relay_host', label: 'Relay host', value: 'ground-station.local' },
            { key: 'freshness_window', label: 'Freshness window', value: '2 s' },
            { key: 'confirmation_window', label: 'Confirmation window', value: '45 s' },
          ],
        },
      ],
      staged_changes: [],
      modes: [
        {
          mode: 'indoor',
          positioning: 'Lighthouse or Loco, optical-flow fallback',
          box: 'defined once per space',
          spacing: '0.8 m',
          speed: '1.2 m/s',
          note: 'the capstone mode',
          status: 'accepted',
        },
        {
          mode: 'outdoorC',
          positioning: 'GPS, RTK optional',
          box: 'polygon plus ceiling',
          spacing: '4 m',
          speed: '4 m/s',
          note: 'designed, not flown',
          status: 'unsupported',
        },
        {
          mode: 'outdoorF',
          positioning: 'GPS plus compass',
          box: 'moving fence around the operator',
          spacing: '6 m',
          speed: '6 m/s',
          note: 'designed, not flown',
          status: 'unsupported',
        },
      ],
    },
  }
}

function departedFive(rosterVersion: number): FixtureDeparture {
  return {
    droneId: 5,
    connectionEpoch: 2,
    rosterVersion,
    ageMs: 402_000,
    reason: 'adapter_connection_lost',
    adapterId: 'sim-05',
  }
}

function designDrone(now: number, id: DroneId, overrides: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return {
    drone_id: id,
    connection_epoch: 1,
    membership: 'ready',
    readiness_reasons: [],
    flight_state: 'hovering',
    battery: 0.82,
    link: 0.94,
    pos_quality: 0.91,
    control_authority: true,
    rc_safety_operator_present: true,
    last_seen_at: now - 1_200,
    camera_patterns: ['pano_360', 'reconstruct_8'],
    selectable: true,
    adapter_id: `sim-${String(id).padStart(2, '0')}`,
    adapter_capabilities: ['flight', 'camera'],
    home_pose: { x: 0, y: 0, z: 0 },
    telemetry: { x: 0.4, y: 1.1, z: 1.4 },
    membership_history: [],
    membership_history_truncated: 0,
    video: { status: 'live', last_frame_at: now - 400 },
    ...overrides,
  }
}

function designFourFleet(now: number): RelayAircraftState[] {
  return [
    designDrone(now, 1, { battery: 0.78, link: 0.92, pos_quality: 0.88 }),
    designDrone(now, 2, {
      battery: 0.64,
      link: 0.81,
      pos_quality: 0.73,
      video: { status: 'offline', last_frame_at: now - 38_000 },
    }),
    designDrone(now, 3, {
      membership: 'degraded',
      readiness_reasons: ['telemetry_stale', 'home_pose_missing'],
      battery: 0.41,
      link: 0.38,
      pos_quality: 0.12,
      selectable: false,
      last_seen_at: now - 9_400,
      video: { status: 'unreported', last_frame_at: null },
    }),
    designDrone(now, 4, {
      connection_epoch: 5,
      membership: 'registered',
      flight_state: 'landed',
      control_authority: false,
      rc_safety_operator_present: false,
      readiness_reasons: ['control_authority_missing', 'rc_safety_operator_missing'],
      selectable: false,
      battery: 0.96,
      link: 0.9,
      pos_quality: 0.81,
    }),
  ]
}

function designSixFleet(now: number): RelayAircraftState[] {
  return [
    designDrone(now, 1, { battery: 0.71, link: 0.88, pos_quality: 0.84 }),
    designDrone(now, 2, {
      battery: 0.64,
      flight_state: 'airborne',
      video: { status: 'offline', last_frame_at: now - 41_000 },
    }),
    designDrone(now, 3, {
      membership: 'degraded',
      readiness_reasons: ['telemetry_stale', 'home_pose_missing'],
      battery: 0.39,
      link: 0.34,
      pos_quality: 0.11,
      selectable: false,
      last_seen_at: now - 9_400,
      video: { status: 'unreported', last_frame_at: null },
    }),
    designDrone(now, 4, {
      connection_epoch: 5,
      membership: 'registered',
      flight_state: 'landed',
      control_authority: false,
      rc_safety_operator_present: false,
      readiness_reasons: ['control_authority_missing', 'rc_safety_operator_missing'],
      selectable: false,
      battery: 0.96,
      link: 0.91,
      pos_quality: 0.77,
    }),
    designDrone(now, 5, {
      connection_epoch: 2,
      membership: 'disconnected',
      flight_state: 'disarmed',
      readiness_reasons: ['disconnected', 'telemetry_missing'],
      selectable: false,
      battery: 0,
      link: 0,
      pos_quality: 0,
      last_seen_at: now - 96_000,
      video: { status: 'offline', last_frame_at: null },
    }),
    designDrone(now, 6, {
      membership: 'leaving',
      flight_state: 'landing',
      readiness_reasons: ['leaving'],
      selectable: false,
      battery: 0.44,
      link: 0.79,
      pos_quality: 0.66,
    }),
  ]
}

export function fixtureAircraft(now: number, fleetSize: FixtureFleetSize = 4): RelayAircraftState[] {
  const fleet: RelayAircraftState[] = [
    {
      drone_id: 1,
      connection_epoch: 3,
      membership: 'ready',
      readiness_reasons: [],
      flight_state: 'hovering',
      battery: 0.78,
      link: 0.96,
      pos_quality: 0.92,
      last_seen_at: now - 120,
      camera_patterns: ['pano_360', 'reconstruct_8'],
      selectable: true,
      adapter_id: 'fixture-dji-01',
      adapter_capabilities: ['flight', 'camera'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 0, y: 0, z: 0 },
      telemetry: { fresh: true },
      membership_history: [],
      membership_history_truncated: 0,
      video: { status: 'live', last_frame_at: now - 180 },
    },
    {
      drone_id: 2,
      connection_epoch: 1,
      membership: 'ready',
      readiness_reasons: [],
      flight_state: 'landed',
      battery: 0.91,
      link: 0.88,
      pos_quality: 0.84,
      last_seen_at: now - 190,
      camera_patterns: ['reconstruct_8'],
      selectable: true,
      adapter_id: 'fixture-dji-02',
      adapter_capabilities: ['flight', 'camera'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 0.8, y: 0, z: 0 },
      telemetry: { fresh: true },
      membership_history: [],
      membership_history_truncated: 0,
      video: { status: 'offline', last_frame_at: now - 12_000 },
    },
    {
      drone_id: 3,
      connection_epoch: 2,
      membership: 'degraded',
      readiness_reasons: ['telemetry_stale', 'camera_not_ready'],
      flight_state: 'landed',
      battery: 0.64,
      link: 0.41,
      pos_quality: 0.75,
      last_seen_at: now - 4_800,
      camera_patterns: [],
      selectable: false,
      adapter_id: 'fixture-dji-03',
      adapter_capabilities: ['flight'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 1.6, y: 0, z: 0 },
      telemetry: { fresh: false },
      membership_history: [],
      membership_history_truncated: 0,
      video: { status: 'offline', last_frame_at: now - 5_100 },
    },
    {
      drone_id: 4,
      connection_epoch: 1,
      membership: 'ready',
      readiness_reasons: [],
      flight_state: 'hovering',
      battery: 0.72,
      link: 0.82,
      pos_quality: 0.88,
      last_seen_at: now - 240,
      camera_patterns: ['pano_360'],
      selectable: true,
      adapter_id: 'fixture-dji-04',
      adapter_capabilities: ['flight', 'camera'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 2.4, y: 0, z: 0 },
      telemetry: { fresh: true },
      membership_history: [],
      membership_history_truncated: 0,
    },
  ]
  if (fleetSize === 4) return fleet
  return [
    ...fleet,
    {
      drone_id: 5,
      connection_epoch: 1,
      membership: 'ready',
      readiness_reasons: [],
      flight_state: 'hovering',
      battery: 0.69,
      link: 0.8,
      pos_quality: 0.86,
      last_seen_at: now - 160,
      camera_patterns: ['pano_360'],
      selectable: true,
      adapter_id: 'fixture-dji-05',
      adapter_capabilities: ['flight', 'camera'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 3.2, y: 0, z: 0 },
      telemetry: { fresh: true },
      membership_history: [],
      membership_history_truncated: 0,
      video: { status: 'live', last_frame_at: now - 140 },
    },
    {
      drone_id: 6,
      connection_epoch: 1,
      membership: 'ready',
      readiness_reasons: [],
      flight_state: 'landed',
      battery: 0.87,
      link: 0.9,
      pos_quality: 0.91,
      last_seen_at: now - 210,
      camera_patterns: ['reconstruct_8'],
      selectable: true,
      adapter_id: 'fixture-dji-06',
      adapter_capabilities: ['flight', 'camera'],
      control_authority: true,
      rc_safety_operator_present: true,
      home_pose: { x: 4, y: 0, z: 0 },
      telemetry: { fresh: true },
      membership_history: [],
      membership_history_truncated: 0,
      video: { status: 'unreported', last_frame_at: null },
    },
  ]
}
