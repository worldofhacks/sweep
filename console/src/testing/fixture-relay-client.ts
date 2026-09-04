import type { RelayClient, RelayClientEvent, RelayClientListener } from '../relay/client'
import type {
  DroneId,
  IntentV1,
  RelayAircraftState,
  RelayServerEvent,
  IntentSource,
} from '../relay/contract'

export type FixtureFleetSize = 4 | 6

export class FixtureRelayClient implements RelayClient {
  readonly transport = 'fixture' as const
  readonly sent: IntentV1[] = []
  private readonly listeners = new Set<RelayClientListener>()
  private rosterVersion = 7
  private selection: DroneId[] = [1]
  private sequence = 0
  private readonly sessionId: string
  private readonly now: () => number
  private readonly source: IntentSource
  private readonly fleetSize: FixtureFleetSize

  constructor(
    sessionId: string,
    now: () => number = () => Date.now(),
    source: IntentSource = 'console',
    fleetSize: FixtureFleetSize = 4,
  ) {
    this.sessionId = sessionId
    this.now = now
    this.source = source
    this.fleetSize = fleetSize
  }

  start(): void {
    this.emit({
      kind: 'connection',
      connection: {
        status: 'connected',
        transport: this.transport,
        changedAt: this.now(),
        reason: 'DEVELOPMENT FIXTURE — no commands leave this browser.',
      },
    })
    this.emitServer({
      v: 1,
      t: this.now(),
      event_id: this.nextEventId(),
      type: 'auth.accepted',
      session: this.sessionId,
      source: this.source,
      drone_id: null,
    })
    this.emitServer({
      v: 1,
      t: this.now(),
      event_id: this.nextEventId(),
      type: 'state',
      session: this.sessionId,
      roster_version: this.rosterVersion,
      armed: true,
      estop: false,
      selection: this.selection,
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: ['hold'],
      pending: null,
      accepted_plan: null,
      drones: fixtureAircraft(this.now(), this.fleetSize),
    })
  }

  stop(): void {}

  subscribe(listener: RelayClientListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    const t = this.now()
    if (intent.name === 'select' && 'ids' in intent.args) {
      this.selection = [...intent.args.ids]
      this.emitServer({
        v: 1,
        t,
        event_id: this.nextEventId(),
        type: 'state',
        session: this.sessionId,
        roster_version: this.rosterVersion,
        armed: true,
        estop: false,
        selection: this.selection,
        formation: 'none',
        spacing: 0.8,
        mode: 'indoor',
        capability_profile: 'c1_basic_control',
        enabled_intent_names: ['hold'],
        pending: null,
        accepted_plan: null,
        drones: fixtureAircraft(this.now(), this.fleetSize),
      })
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
      roster_version: this.rosterVersion,
    })
  }

  emitServer(event: RelayServerEvent): void {
    this.emit({ kind: 'server_event', event })
  }

  emitConnection(status: 'connected' | 'degraded' | 'disconnected', reason?: string): void {
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

  private nextEventId(): string {
    this.sequence += 1
    return `fixture-${this.source}-event-${this.sequence}`
  }

  private emit(event: RelayClientEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }
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
