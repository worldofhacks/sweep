import type { RelayClient, RelayClientEvent, RelayClientListener } from '../relay/client'
import type {
  DroneId,
  IntentV1,
  RelayAircraftState,
  RelayServerEvent,
  IntentSource,
} from '../relay/contract'

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

  constructor(
    sessionId: string,
    now: () => number = () => Date.now(),
    source: IntentSource = 'console',
    scenario: FixtureFleetSize | FixtureScenarioName = 4,
  ) {
    this.sessionId = sessionId
    this.now = now
    this.source = source
    this.scenario = typeof scenario === 'number' ? controlScenario(scenario) : fixtureScenario(scenario)
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
    if (intent.name === 'select' && 'ids' in intent.args) {
      this.selection = [...intent.args.ids]
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
      armed: true,
      estop: false,
      selection: this.selection,
      formation: this.scenario.formation,
      spacing: this.scenario.spacing,
      mode: 'indoor',
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
      }
    }
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
      video: { status: 'unreported', last_frame_at: null },
    },
  ]
}
