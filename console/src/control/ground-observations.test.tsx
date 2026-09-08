import { act, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import App from '../App'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1, isSupportedIntent, parseRelayServerEvent, type ConsoleIntentName, type RelayStateEvent } from '../relay/contract'
import { parseObservation, type Observation } from '../relay/observation'
import { relayMediaConfigurationSource } from '../media/runtime-config'
import { deriveStream } from '../modules/live/derive-live'
import { mapDevices } from '../modules/map/derive-map'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { controlReducer, createInitialControlState, formatDeviceId, isIntentEnabled, type ControlState } from './state'
import { motionObservationCurrent, observedControlState } from './observation'

const t = 1788790000000
const session = 'isolated-ground-diagnostics'

function stateFrame(overrides: Record<string, unknown> = {}): RelayStateEvent {
  const parsed = parseRelayServerEvent({ v: 1, type: 'state', t, event_id: 'ground-state', session,
    roster_version: 1, armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control.ground', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'ground_velocity', 'survey_area'],
    pending: null, accepted_plan: null, drones: [{ ...fixtureAircraft(t)[0], drone_id: 11,
      node_type: 'ground', device_class: 'ground_vehicle', unit: 1, connection_epoch: 1,
      membership: 'degraded', selectable: false, control_authority: false, rc_safety_operator_present: false,
      readiness_reasons: ['drive_authority_missing'], adapter_capabilities: ['ground_drive', 'screen', 'lidar'],
      flight_state: null, telemetry: null, battery: null, link: null, pos_quality: null, last_seen_at: null,
      node_status: null, camera_capabilities: null, ground_readiness: { source_id: 'ohmni-pose' },
      video: { status: 'live', last_frame_at: t } }], ...overrides })
  if (parsed?.type !== 'state') throw new Error('isolated state fixture rejected')
  return parsed
}

function observation(kind: 'pose' | 'telemetry' | 'status' | 'range_scan' = 'telemetry', overrides: Record<string, unknown> = {}): Observation {
  const pose = { parent_frame: 'odom', child_frame: kind === 'range_scan' ? 'lidar' : 'body', x_m: 1.25, y_m: -0.5, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 }
  const payload = kind === 'pose' ? { kind, pose }
    : kind === 'telemetry' ? { kind, position: { frame: 'odom', x_m: 1.25, y_m: -0.5, z_m: 0 },
      velocity: { frame: 'odom', x_m_s: 0, y_m_s: 0, z_m_s: 0 }, battery: 0.7, link: 1, pos_quality: 0, state: 'stopped' }
      : kind === 'range_scan' ? { kind, sensor_pose: pose, angle_min_rad: 0, angle_increment_rad: 0.1,
        range_min_m: 0.1, range_max_m: 20, ranges_m: [null, 2, 3], mount_id: 'isolated-mount' }
        : { kind, code: 'ground_runtime', detail: 'stopped', capabilities: ['ground_drive'] }
  const parsed = parseObservation({ v: 1, type: 'observation', event_id: `ground-${kind}`, session, device_id: 11,
    connection_epoch: 1, source_id: `ohmni-${kind}`, node_type: 'ground', frame: kind === 'range_scan' ? 'lidar' : 'odom',
    confidence: 0, t_capture: null, t_source_receipt: { clock_id: 'ohmni-monotonic', unit: 'ms', value: 1000 },
    clock_mapping_id: null, payload, t_ingest: t, ...overrides })
  if (!parsed) throw new Error('isolated observation fixture rejected')
  return parsed
}

function readyState(): ControlState {
  let state = createInitialControlState(session, t)
  state = controlReducer(state, { type: 'connection_changed', connection: { status: 'connected', transport: 'fixture', changedAt: t } })
  return controlReducer(state, { type: 'relay_event', event: stateFrame(), receivedAt: t })
}

function accept(state: ControlState, event: Observation, now = t) {
  return controlReducer(state, { type: 'relay_event', event, receivedAt: now })
}

afterEach(() => vi.useRealTimers())

describe('read-only field ground compatibility', () => {
  test('uses the configured display identity and recognizes advertisements without implementing commands', () => {
    const state = readyState()
    expect(formatDeviceId(state.aircraft[11])).toBe('G-01')
    expect(state.aircraft[11].node_type).toBe('ground')
    expect(state.enabledIntentNames).toContain('ground_velocity')
    for (const name of ['survey_area']) {
      expect(isSupportedIntent(name as ConsoleIntentName)).toBe(false)
      expect(isIntentEnabled(state, name as ConsoleIntentName)).toBe(false)
      expect(isConsoleIntentV1({ v: 1, type: 'intent', t, intent_id: 'unsupported', retry_of: null, source: 'console', session, name,
        args: { linear_mm_s: 1, angular_mrad_s: 0, duration_ms: 100 }, selection: [11], mode: 'indoor', confirm: true })).toBe(false)
    }
    expect(state.armed).toBe(false)
    expect(state.selection).toEqual([])
  })

  test('derives ground class only from an explicit node type and rejects contradictory identity', () => {
    const raw = stateFrame()
    const ground: Record<string, unknown> = { ...raw.drones[0] }
    delete ground.device_class
    delete ground.unit
    const parsed = parseRelayServerEvent({ ...raw, drones: [ground] })
    expect(parsed?.type === 'state' && parsed.drones[0].device_class).toBe('ground_vehicle')
    expect(parseRelayServerEvent({ ...raw, drones: [{ ...ground, device_class: 'aircraft' }] })).toBeNull()
    expect(parseRelayServerEvent({ ...raw, drones: [{ ...ground, node_type: 'unknown' }] })).toBeNull()
    expect(parseRelayServerEvent({ ...raw, enabled_intent_names: [...raw.enabled_intent_names, 'unknown_operation'] })).toBeNull()
  })

  test('retains authenticated local observations without creating motion or world telemetry', () => {
    let state = accept(readyState(), observation())
    state = accept(state, observation('pose', { confidence: 0.8 }))
    state = accept(state, observation('range_scan'))
    const device = observedControlState(state, t).aircraft[11]
    expect(device.client_observation?.state).toBe('current')
    expect(device.client_observation?.ground?.poseCurrent).toBe(true)
    expect(device.client_observation?.ground?.scan?.payload.kind).toBe('range_scan')
    expect(device.telemetry).toBeNull()
    expect(device.control_authority).toBe(false)
    expect(motionObservationCurrent(device)).toBe(false)
    expect(mapDevices([device], { latest: {}, trails: {} }, t)).toEqual([])
  })

  test.each([{ session: 'other' }, { connection_epoch: 2 }, { device_id: 12 }, { node_type: 'aircraft' },
    { t_ingest: t + 1001 }, { t_ingest: t - 30_001 }])('ignores mismatched or outside-window observations %j', (patch) => {
    const state = readyState()
    expect(accept(state, observation('telemetry', patch))).toBe(state)
  })

  test('bounds storage, rejects reordered replacements and retires evidence on rejoin and reconnect', () => {
    let state = readyState()
    for (let index = 0; index < 300; index += 1) state = accept(state, observation('status', { source_id: `source-${index}`, t_ingest: t + index }), t + 300)
    expect(Object.keys(state.latestObservations)).toHaveLength(256)
    const same = accept(state, observation('status', { source_id: 'source-299', t_ingest: t + 298 }), t + 300)
    expect(same).toBe(state)
    const next = stateFrame({ event_id: 'new-epoch', t: t + 301, roster_version: 2,
      drones: [{ ...state.aircraft[11], connection_epoch: 2 }] })
    state = controlReducer(state, { type: 'relay_event', event: next, receivedAt: t + 301 })
    expect(state.latestObservations).toEqual({})
    state = accept(state, observation('status', { connection_epoch: 2, t_ingest: t + 301 }), t + 301)
    expect(Object.keys(state.latestObservations)).toHaveLength(1)
    state = controlReducer(state, { type: 'connection_changed', connection: { status: 'connecting', transport: 'fixture', changedAt: t + 302 } })
    expect(state.latestObservations).toEqual({})
  })

  test('keeps fresh ground camera evidence usable independently of pose, then expires it', () => {
    const state = readyState()
    expect(deriveStream(observedControlState(state, t).aircraft[11], t).status).toBe('live')
    expect(deriveStream(observedControlState(state, t + 5001).aircraft[11], t + 5001).status).toBe('unreported')
    for (const last_frame_at of [null, t + 1]) {
      const device = observedControlState(state, t).aircraft[11]
      expect(deriveStream({ ...device, video: { status: 'live', last_frame_at } }, t).status).toBe('unreported')
    }
    const disconnected = { ...state, connection: { ...state.connection, status: 'disconnected' as const } }
    expect(deriveStream(observedControlState(disconnected, t).aircraft[11], t).status).toBe('unreported')
  })

  test('preserves a path-proxied media endpoint and bearer', () => {
    expect(relayMediaConfigurationSource('wss://relay.example/field/', 'isolated-token')).toEqual({
      url: 'https://relay.example/field/runtime-config.json', authorization: 'Bearer isolated-token',
    })
  })

  test('shows real-contract ground diagnostics and stale values without sending commands', async () => {
    vi.useFakeTimers()
    let now = t
    const clients = { console: new FixtureRelayClient(session, () => now), keyboard: new FixtureRelayClient(session, () => now, 'keyboard') }
    render(<App sessionId={session} clients={clients} initialModule="devices" intentDependencies={{ now: () => now, nextId: () => 'unused' }} />)
    act(() => { clients.console.emitServer(stateFrame({ roster_version: 100 })) })
    for (const event of [observation(), observation('pose'), observation('range_scan')]) {
      act(() => { clients.console.emitServer(event) })
    }
    const card = within(screen.getByRole('article', { name: 'G-01 device card' }))
    expect(card.getByText(/Authenticated ground observations · wire ID 11 · epoch 1/)).toBeInTheDocument()
    expect(card.getByText(/Local odometry · odom · x 1.25 m/)).toBeInTheDocument()
    expect(card.getByText(/Current telemetry/)).toHaveTextContent('link transport receipt reported · radio quality unreported')
    expect(card.queryByText(/link 100%/)).not.toBeInTheDocument()
    expect(card.getByText(/Current LiDAR · lidar · 2\/3 returns · closest 2.00 m/)).toBeInTheDocument()
    expect(card.getByText(/current pose unavailable/)).toBeInTheDocument()
    now += 6000
    act(() => { vi.advanceTimersByTime(6000) })
    expect(card.getByText(/Last reported telemetry · stale/)).toHaveTextContent('transport receipt reported · radio quality unreported')
    expect(card.queryByText(/link 100%/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ground velocity|survey area/i })).not.toBeInTheDocument()
    expect(clients.console.sent).toEqual([])
    expect(clients.keyboard.sent).toEqual([])
  })
})
