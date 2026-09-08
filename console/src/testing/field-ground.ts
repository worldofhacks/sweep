/** Isolated signed-contract fixtures; never imported by application runtime code. */
import { C1_BASIC_CONTROL_INTENTS, parseRelayServerEvent, type RelayStateEvent } from '../relay/contract'
import { parseObservation, type Observation } from '../relay/observation'
import { fixtureAircraft } from './fixture-relay-client'

export function fieldGroundState(now: number, session: string, patch: Partial<RelayStateEvent> = {}): RelayStateEvent {
  const value = parseRelayServerEvent({ v: 1, type: 'state', t: now, event_id: `state-${now}`, session, roster_version: 100,
    armed: false, estop: false, selection: [11], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control.ground', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'ground_velocity'],
    pending: null, accepted_plan: null, drones: [{ ...fixtureAircraft(now)[0], drone_id: 11, node_type: 'ground', device_class: 'ground_vehicle', unit: 1,
      connection_epoch: 3, membership: 'ready', selectable: true, control_authority: true, rc_safety_operator_present: true,
      readiness_reasons: [], adapter_capabilities: ['ground_drive', 'lidar'], ground_readiness: { source_id: 'isolated-pose' },
      flight_state: null, telemetry: null, battery: null, link: null, pos_quality: null, last_seen_at: null, node_status: null, camera_capabilities: null,
    }], ...patch })
  if (value?.type !== 'state') throw new Error('Invalid isolated field state')
  return value
}

export function fieldGroundPose(now: number, session: string, patch: Partial<Observation> = {}): Observation {
  const value = parseObservation({ v: 1, type: 'observation', event_id: `pose-${now}`, session, device_id: 11,
    connection_epoch: 3, source_id: 'isolated-pose', node_type: 'ground', frame: 'odom', confidence: 0.8,
    t_capture: null, t_source_receipt: { clock_id: 'isolated-clock', unit: 'ms', value: 100 }, clock_mapping_id: null,
    payload: { kind: 'pose', pose: { parent_frame: 'odom', child_frame: 'body', x_m: 1, y_m: 1, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } }, t_ingest: now, ...patch })
  if (!value) throw new Error('Invalid isolated field pose')
  return value
}
