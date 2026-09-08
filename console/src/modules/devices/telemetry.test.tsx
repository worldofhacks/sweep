import { render, screen, within } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import { controlReducer, createInitialControlState } from '../../control/state'
import { C1_BASIC_CONTROL_INTENTS, isDeviceTelemetry, parseRelayServerEvent, type RelayAircraftState, type RelayStateEvent } from '../../relay/contract'
import { publicNodeEvents } from '../../testing/public-node-events'
import { commandCatalog } from '../control/controls'
import { DeviceTelemetryPanel } from './DeviceTelemetryPanel'
import { captureGuidance, deviceTelemetryRows } from './telemetry'

const session = 'telemetry-console'
const now = 1788726306375
const [camera, node, guidance] = publicNodeEvents(session, 5)
const custom = {
  adapter: 'ohmni', source: 'hardware',
  lidar: { present: true, model: 'RPLIDAR A2M8', status: 'scanning', health: 'good', motor_running: true,
    calibrated: false, frame: 'sensor_native', scan_age_ms: 25, valid_bins: 317, sectors: { front: 0.6 }, nearest_m: 0.18, ranges_cm: [0, 18, 60, 100] },
  safety: { blocked: true, reasons: ['obstacle_near'], motion_enabled: false },
  controls: { supported_operations: ['goto', 'hover'], unsupported_operations: ['takeoff'] },
  vendor_extension: { sensor_board_revision: 'rev_b', omitted_measurement: null },
}
function device(patch: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return { drone_id: 2, connection_epoch: 5, device_class: 'ground_vehicle', unit: 1,
    membership: 'degraded', readiness_reasons: ['control_authority_missing'], flight_state: 'idle',
    battery: 0.7, link: 0.9, pos_quality: 0.8, control_authority: false, rc_safety_operator_present: true,
    last_seen_at: now, camera_patterns: [], selectable: false, adapter_id: 'ohmni-1', adapter_capabilities: ['ground_drive', 'lidar'],
    home_pose: { x: 0, y: 0, z: 0 }, telemetry: { t: now, x: 1, y: 2, z: 0, vx: 0, vy: 0, vz: 0 },
    membership_history: [], membership_history_truncated: 0, node_status: { ...node, device_telemetry: custom }, ...patch }
}
function stateEvent(drones = [device()]): RelayStateEvent {
  return { v: 1, t: now, type: 'state', event_id: 'snapshot', session, roster_version: 1,
    armed: false, estop: false, selection: [], formation: 'none', spacing: 1, mode: 'indoor',
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'body_pulse'], pending: null, accepted_plan: null, drones }
}

describe('complete fleet telemetry integration', () => {
  test('parses the projected reports and bounded extension without dropping custom groups', () => {
    const projected = (event: typeof node | typeof camera) => Object.fromEntries(Object.entries(event).filter(([key]) => !['event_id', 'session', 'connection_epoch'].includes(key)))
    const wire = { ...stateEvent(), drones: [{ ...device(), node_status: { ...projected(node), device_telemetry: custom }, camera_capabilities: projected(camera) }] }
    const parsed = parseRelayServerEvent(wire)
    expect(parsed).toEqual({ ...wire, drones: wire.drones.map((drone) => ({ ...drone, node_type: 'ground' })) })
    expect(parseRelayServerEvent({ ...wire, drones: [{ ...wire.drones[0], node_status: { ...wire.drones[0].node_status, phone_battery_percent: 101 } }] })).toBeNull()
    expect(parseRelayServerEvent({ ...node, device_telemetry: custom })).toEqual({ ...node, device_telemetry: custom })
  })
  test('enforces finite, bounded JSON and the backend depth boundary', () => {
    expect(isDeviceTelemetry({ a: { b: { c: 1 } } })).toBe(true)
    expect(isDeviceTelemetry({ a: { b: { c: { d: 1 } } } })).toBe(false)
    for (const invalid of [{ a: { b: { c: { d: { e: 1 } } } } }, { bad_key: Infinity }, { wrongCase: 1 },
      { values: Array(513).fill(1) }, { label: 'x'.repeat(513) },
      Object.fromEntries(Array.from({ length: 33 }, (_, i) => [`key_${i}`, 'x'.repeat(512)])),
      { nested: { invalid: undefined } }]) expect(isDeviceTelemetry(invalid)).toBe(false)
  })
  test('shows the exact robot blocker, raw sensor frame and every extra field in the inspector', () => {
    render(<DeviceTelemetryPanel device={device()} now={now + 500} />)
    expect(screen.getByText('blocked · obstacle_near')).toBeInTheDocument()
    expect(screen.getByText('525 ms')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'G-01 raw LiDAR returns by sensor bin' })).toBeInTheDocument()
    expect(screen.getByText(/robot orientation uncalibrated/)).toBeInTheDocument()
    const complete = screen.getByLabelText('G-01 complete telemetry')
    expect(JSON.parse(complete.textContent ?? '{}').node_status.device_telemetry).toEqual(custom)
    expect(screen.queryByText(/RC override/)).not.toBeInTheDocument()
  })
  test('stale and missing health never appears clear; zeros remain reported values', () => {
    const fresh = device({ node_status: { ...node, device_telemetry: { safety: { blocked: false }, lidar: { scan_age_ms: 0, valid_bins: 0 } } } })
    expect(deviceTelemetryRows(fresh, now + 6000).find((row) => row.label === 'obstacle avoidance')?.value).toMatch(/stale.*unknown/)
    expect(deviceTelemetryRows(fresh, now).find((row) => row.label === 'LiDAR scan age')?.value).toBe('0 ms')
    expect(deviceTelemetryRows(device({ node_status: null }), now).find((row) => row.label === 'watchdog')?.value).toBe('unreported')
  })
  test('retains current-epoch public facts without enabling controls and drops old/reordered reports', () => {
    let state = controlReducer(createInitialControlState(session, now), { type: 'relay_event', event: stateEvent([device({ node_status: null })]) })
    const report = { ...node, event_id: 'new-health', t: now + 1, control_authority: true, device_telemetry: custom }
    state = controlReducer(state, { type: 'relay_event', event: report })
    expect(state.aircraft[2].node_status).toEqual(report)
    expect(state.aircraft[2].control_authority).toBe(false)
    expect(state.aircraft[2].selectable).toBe(false)
    for (const patch of [{ connection_epoch: 4 }, { connection_epoch: 6 }, { t: now - 1 }]) {
      const next = controlReducer(state, { type: 'relay_event', event: { ...report, ...patch, event_id: JSON.stringify(patch) } })
      expect(next.aircraft).toBe(state.aircraft)
    }
    state = controlReducer(state, { type: 'relay_event', event: { ...stateEvent([device({ connection_epoch: 6, node_status: null })]), t: now + 2, event_id: 'rejoin' } })
    expect(state.aircraft[2].node_status).toBeNull()
  })
  test('drone hardware and phone status are visible and capture advice expires without claiming coverage', () => {
    const drone = device({ device_class: 'aircraft', unit: 2, camera_capabilities: camera, capture_readiness: { ...guidance, room_id: 'lab', coverage_missing: [0] }, node_status: { ...node, phone_battery_percent: 0 } })
    render(<DeviceTelemetryPanel device={drone} now={now} />)
    const panel = within(screen.getByRole('region', { name: 'D-02 telemetry and diagnostics' }))
    expect(panel.getByText('0% · none')).toBeInTheDocument()
    expect(panel.getByText(/photo unsupported · retrieval unsupported/)).toBeInTheDocument()
    expect(captureGuidance(drone, 'lab', now)?.coverage).toEqual(['unseen', ...Array(7).fill('unreported')])
    expect(captureGuidance(drone, 'other-room', now)).toBeNull()
    expect(captureGuidance(drone, 'lab', now + 5001)).toBeNull()
    expect(captureGuidance({ ...drone, connection_epoch: 6 }, 'lab', now)).toBeNull()
  })
  test('bounded flight controls stay visible but unsupported for robot selection', () => {
    const initial = createInitialControlState(session, now)
    const state = { ...initial, aircraft: { 2: device() }, selection: [2], capabilityProfile: 'c1_basic_control',
      enabledIntentNames: [...C1_BASIC_CONTROL_INTENTS, 'body_pulse' as const], connection: { ...initial.connection, status: 'connected' as const } }
    const pulses = commandCatalog(state).flatMap((group) => group.rows).filter((row) => row.intent === 'body_pulse')
    expect(pulses).toHaveLength(2)
    expect(pulses.every((row) => !row.enabled && row.status === 'unsupported')).toBe(true)
  })
})
