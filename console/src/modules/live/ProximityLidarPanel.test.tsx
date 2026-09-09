import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { C1_BASIC_CONTROL_INTENTS, parseRelayServerEvent, type RelayStateEvent } from '../../relay/contract'
import { parseObservation, type Observation } from '../../relay/observation'
import { fixtureAircraft } from '../../testing/fixture-relay-client'
import { controlReducer, createInitialControlState, type ControlState } from '../../control/state'
import { observedControlState } from '../../control/observation'
import { ProximityLidarPanel } from './ProximityLidarPanel'

const t = 1788790000000
const session = 'proximity-lidar-panel'

function stateFrame(overrides: Record<string, unknown> = {}): RelayStateEvent {
  const parsed = parseRelayServerEvent({ v: 1, type: 'state', t, event_id: 'ground-state', session,
    roster_version: 1, armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control.ground', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'ground_velocity', 'survey_area'],
    pending: null, accepted_plan: null, drones: [{ ...fixtureAircraft(t)[0], drone_id: 11,
      node_type: 'ground', device_class: 'ground_vehicle', unit: 1, connection_epoch: 1,
      membership: 'ready', selectable: false, control_authority: false, rc_safety_operator_present: false,
      readiness_reasons: [], adapter_capabilities: ['ground_drive', 'screen', 'lidar'],
      flight_state: null, telemetry: null, battery: null, link: null, pos_quality: null, last_seen_at: null,
      node_status: null, camera_capabilities: null, ground_readiness: { source_id: 'ohmni-pose' },
      video: { status: 'live', last_frame_at: t } }], ...overrides })
  if (parsed?.type !== 'state') throw new Error('isolated state fixture rejected')
  return parsed
}

function rangeScanObservation(rangesM: (number | null)[], overrides: Record<string, unknown> = {}): Observation {
  const pose = { parent_frame: 'odom', child_frame: 'lidar', x_m: -0.268, y_m: 0.0999, z_m: 0.6096, qx: 0, qy: 0, qz: 0, qw: 1 }
  const payload = { kind: 'range_scan' as const, sensor_pose: pose, angle_min_rad: 0, angle_increment_rad: (2 * Math.PI) / rangesM.length,
    range_min_m: 0.1, range_max_m: 20, ranges_m: rangesM, mount_id: 'proximity-test-mount' }
  const parsed = parseObservation({ v: 1, type: 'observation', event_id: 'ground-range-scan', session, device_id: 11,
    connection_epoch: 1, source_id: 'ohmni-range-scan', node_type: 'ground', frame: 'lidar',
    confidence: 0, t_capture: null, t_source_receipt: { clock_id: 'ohmni-monotonic', unit: 'ms', value: 1000 },
    clock_mapping_id: null, payload, t_ingest: t, ...overrides })
  if (!parsed) throw new Error('isolated range scan fixture rejected')
  return parsed
}

function readyState(): ControlState {
  let state = createInitialControlState(session, t)
  state = controlReducer(state, { type: 'connection_changed', connection: { status: 'connected', transport: 'fixture', changedAt: t } })
  return controlReducer(state, { type: 'relay_event', event: stateFrame(), receivedAt: t })
}

test('renders no-scan state when the device has never reported one', () => {
  render(<ProximityLidarPanel device={observedControlState(readyState(), t).aircraft[11]} now={t} />)
  expect(screen.getByText('No LiDAR scan reported for this device.')).toBeInTheDocument()
})

test('colours near, caution and clear returns and warns about missing bearings and self-returns', () => {
  const state = controlReducer(readyState(), { type: 'relay_event', event: rangeScanObservation([0.18, 0.6, 5, null]), receivedAt: t })
  render(<ProximityLidarPanel device={observedControlState(state, t).aircraft[11]} now={t} />)
  expect(screen.getByRole('img', { name: 'G-01 LiDAR returns coloured by distance' }).querySelectorAll('.lv-lidar-pt.is-near')).toHaveLength(1)
  expect(screen.getByRole('img', { name: 'G-01 LiDAR returns coloured by distance' }).querySelectorAll('.lv-lidar-pt.is-caution')).toHaveLength(1)
  expect(screen.getByRole('img', { name: 'G-01 LiDAR returns coloured by distance' }).querySelectorAll('.lv-lidar-pt.is-clear')).toHaveLength(1)
  expect(screen.getByText(/3\/4 bearings returned \(75%\)/)).toHaveTextContent('closest 0.18m')
  expect(screen.getByText(/of bearings returned no echo/)).toHaveTextContent('25%')
  expect(screen.getByText(/Self-return filtering is not enabled/)).toBeInTheDocument()
})

test('marks the scan stale once it falls outside the freshness window', () => {
  const state = controlReducer(readyState(), { type: 'relay_event', event: rangeScanObservation([1]), receivedAt: t })
  render(<ProximityLidarPanel device={observedControlState(state, t + 1001).aircraft[11]} now={t + 1001} />)
  expect(screen.getByText('Scan stale or disconnected')).toBeInTheDocument()
})
