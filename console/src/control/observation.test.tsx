import { act, render, renderHook, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, test, vi } from 'vitest'
import App from '../App'
import type { RelayAircraftState } from '../relay/contract'
import { C1_BASIC_CONTROL_INTENTS, parseRelayServerEvent } from '../relay/contract'
import type { Observation } from '../relay/observation'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { deriveStream } from '../modules/live/derive-live'
import { dpadBlockedReason, motionStateWord } from '../modules/control/controls'
import { nodeCells } from '../catalog/derive'
import { mapDevices } from '../modules/map/derive-map'
import { isReady } from '../shell/derive'
import { createInitialControlState } from './state'
import { motionObservationCurrent, observeDevice, observedControlState } from './observation'
import { useControlConsole } from './use-control-console'

const relayNow = 1_756_700_000_000
const sessionId = 'freshness-test'
const aircraft = (overrides: Partial<RelayAircraftState> = {}): RelayAircraftState => ({ ...fixtureAircraft(relayNow)[0], ...overrides })
const observed = (device: RelayAircraftState, now = relayNow) => ({ ...device, client_observation: observeDevice(device, now) })

afterEach(() => vi.useRealTimers())

describe('current and retained device observations', () => {
  test.each([
    ['valid', 0.9, relayNow, true],
    ['missing confidence', 0, relayNow, false],
    ['stale', 0.9, relayNow - 5_001, false],
  ])('a %s canonical ground pose governs ground motion without legacy telemetry', (_label, confidence, t_ingest, ready) => {
    const ground = aircraft({
      drone_id: 11, node_type: 'ground', device_class: 'ground_vehicle', unit: 11, connection_epoch: 1,
      membership: 'ready', selectable: true, telemetry: null, last_seen_at: null,
      ground_readiness: { source_id: 'ohmni-pose' },
    })
    const pose: Observation = {
      v: 1, type: 'observation', event_id: 'ground-pose', session: sessionId, device_id: 11, connection_epoch: 1,
      source_id: 'ohmni-pose', node_type: 'ground', frame: 'world', confidence, t_capture: null,
      t_source_receipt: { clock_id: 'ohmni-ms', unit: 'ms', value: relayNow }, clock_mapping_id: null,
      payload: { kind: 'pose', pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 1, y_m: 2, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } }, t_ingest,
    }
    const reported = {
      ...createInitialControlState(sessionId, relayNow),
      connection: { status: 'connected' as const, transport: 'websocket' as const, changedAt: relayNow },
      aircraft: { 11: ground }, latestObservations: { pose },
      lastStateEvent: { t: relayNow, receivedAt: relayNow, rosterVersion: 1, source: 'console' as const },
    }
    const current = observedControlState(reported, relayNow).aircraft[11]
    expect(current.node_type).toBe('ground')
    expect(current.client_observation?.ground).toMatchObject({ poseCurrent: ready })
    expect(motionObservationCurrent(current)).toBe(ready)
    expect(isReady(current)).toBe(ready)
  })

  test.each(['disconnected', 'leaving'] as const)('a %s device never displays retained hovering or video as current', (membership) => {
    const device = observed(aircraft({ membership }))
    expect(device.client_observation.state).toBe('offline')
    expect(isReady(device)).toBe(false)
    expect(motionStateWord(device)).toBe('offline · last reported hovering')
    expect(deriveStream(device, relayNow).status).toBe('offline')
    expect(device.flight_state).toBe('hovering')
  })

  test.each([null, relayNow + 1])('a missing or future last-seen timestamp (%s) is unknown', (last_seen_at) => {
    const device = observed(aircraft({ last_seen_at }))
    expect(device.client_observation.state).toBe('unknown')
    expect(isReady(device)).toBe(false)
    expect(deriveStream(device, relayNow).status).toBe('unreported')
  })

  test.each([null, {}, { fresh: false }, { t: relayNow - 5_001 }, { t: relayNow + 1 }])('fresh bridge reports do not refresh missing/stale motion telemetry %j', (telemetry) => {
    const device = observed(aircraft({ telemetry }))
    expect(device.client_observation.state).toBe('current')
    expect(motionObservationCurrent(device)).toBe(false)
    expect(isReady(device)).toBe(false)
    expect(motionStateWord(device)).toBe('current motion unknown · last reported hovering')
  })

  test('current timestamped telemetry and the legacy explicit fresh flag are supported', () => {
    expect(motionObservationCurrent(observed(aircraft({ telemetry: { t: relayNow } })))).toBe(true)
    expect(motionObservationCurrent(observed(aircraft({ telemetry: { fresh: true } })))).toBe(true)
  })

  test('fresh bridge reports never refresh stale or timestamp-free video', () => {
    for (const last_frame_at of [null, relayNow - 5_001, relayNow + 1]) {
      expect(deriveStream(observed(aircraft({ video: { status: 'live', last_frame_at } })), relayNow).status).toBe('unreported')
    }
  })

  test('Health and Map do not advertise retained reports as current after disconnect', () => {
    const device = observed(aircraft({ membership: 'disconnected', telemetry: { t: relayNow, x: 1, y: 2 } }))
    const cells = Object.fromEntries(nodeCells(device, null, relayNow).map((cell) => [cell.key, cell.value]))
    expect(cells.Relay).toBe('disconnected')
    expect(cells.Video).toMatch(/^offline/)
    expect(cells.Camera).toBe('current camera state unknown')
    expect(cells.Telemetry).toBe('current telemetry unknown')
    expect(mapDevices([device], { latest: {}, trails: {} }, relayNow)).toEqual([])
  })

  test('relay-clock freshness advances from local receipt despite a browser clock offset', () => {
    const reported = {
      ...createInitialControlState(sessionId, 10_000),
      connection: { status: 'connected' as const, transport: 'websocket' as const, changedAt: 10_000 },
      aircraft: { 1: aircraft() },
      lastStateEvent: { t: relayNow, receivedAt: 10_000, rosterVersion: 1, source: 'console' as const },
    }
    expect(observedControlState(reported, 10_000).aircraft[1].client_observation?.state).toBe('current')
    expect(observedControlState(reported, 16_000).aircraft[1].client_observation?.state).toBe('stale')
    const disconnected = observedControlState({ ...reported, connection: { ...reported.connection, status: 'disconnected' } }, 10_000)
    expect(disconnected.aircraft[1].client_observation?.state).toBe('unknown')
    expect(reported.aircraft[1].client_observation).toBeUndefined()
  })

  test('fresh media survives a stale ground pose but expires with the relay-clock estimate', () => {
    const ground = aircraft({
      drone_id: 11, node_type: 'ground', device_class: 'ground_vehicle', unit: 11, connection_epoch: 1,
      membership: 'ready', selectable: false, telemetry: null, last_seen_at: null,
      video: { status: 'live', last_frame_at: relayNow + 6_000 },
      ground_readiness: { source_id: 'ohmni-pose' },
    })
    const pose: Observation = {
      v: 1, type: 'observation', event_id: 'stale-ground-pose', session: sessionId, device_id: 11, connection_epoch: 1,
      source_id: 'ohmni-pose', node_type: 'ground', frame: 'odom', confidence: 0,
      t_capture: null, t_source_receipt: { clock_id: 'ohmni-ms', unit: 'ms', value: relayNow },
      clock_mapping_id: null,
      payload: { kind: 'pose', pose: { parent_frame: 'odom', child_frame: 'base_link', x_m: 0, y_m: 0, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } },
      t_ingest: relayNow,
    }
    const reported = {
      ...createInitialControlState(sessionId, 16_000),
      connection: { status: 'connected' as const, transport: 'websocket' as const, changedAt: 16_000 },
      aircraft: { 11: ground }, latestObservations: { pose },
      lastStateEvent: { t: relayNow + 6_000, receivedAt: 16_000, rosterVersion: 1, source: 'console' as const },
    }

    const stalePose = observedControlState(reported, 16_000).aircraft[11]
    expect(stalePose.client_observation?.state).toBe('stale')
    expect(deriveStream(stalePose, 16_000).status).toBe('live')

    const agedSnapshot = observedControlState(reported, 22_001).aircraft[11]
    expect(deriveStream(agedSnapshot, 22_001).status).toBe('unreported')

    const relayUnavailable = observedControlState({
      ...reported,
      connection: { ...reported.connection, status: 'disconnected' },
    }, 16_000).aircraft[11]
    expect(relayUnavailable.client_observation?.state).toBe('unknown')
    expect(deriveStream(relayUnavailable, 16_000).status).toBe('live')
  })
})

function clients(now: () => number) {
  return {
    console: new FixtureRelayClient(sessionId, now, 'console'),
    keyboard: new FixtureRelayClient(sessionId, now, 'keyboard'),
  }
}

test('a powered-off device becomes stale without another relay frame and cannot send motion', () => {
  vi.useFakeTimers()
  let now = relayNow
  const peers = clients(() => now)
  const deps = { now: () => now, nextId: () => 'stale-motion' }
  const { result } = renderHook(() => useControlConsole({ sessionId, clients: peers, intentDependencies: deps }))
  expect(isReady(result.current.state.aircraft[1])).toBe(true)
  // The operator clicks before the next render tick; send-time freshness must still win.
  now += 6_000
  act(() => { result.current.issueIntent({ name: 'translate', args: { dx: 1, dy: 0 } }) })
  expect(peers.console.sent).toEqual([])
  expect(result.current.state.requests[0]).toMatchObject({ status: 'failed', detail: expect.stringContaining('Current target motion telemetry') })
  act(() => vi.advanceTimersByTime(1_000))
  expect(result.current.state.aircraft[1].client_observation?.state).toBe('stale')
  expect(dpadBlockedReason(result.current.state)).toContain('Current target motion telemetry')
  expect(result.current.state.selection).toEqual([1])
  expect(result.current.state.aircraft[1].flight_state).toBe('hovering')
})

test('Ground controls enable only after a current accepted canonical pose', async () => {
  const peers = clients(() => relayNow)
  const user = userEvent.setup()
  render(<App sessionId={sessionId} clients={peers} intentDependencies={{ now: () => relayNow, nextId: () => 'ground-pulse' }} />)
  await screen.findByText('1 of 4 selected')
  const ground = aircraft({
    drone_id: 11, node_type: 'ground', device_class: 'ground_vehicle', unit: 11, connection_epoch: 1,
    membership: 'ready', selectable: true, telemetry: null, last_seen_at: null,
    ground_readiness: { source_id: 'ohmni-pose' }, adapter_capabilities: ['ground_drive'],
  })
  const state = parseRelayServerEvent({
    v: 1, t: relayNow, type: 'state', event_id: 'root-ground-state', session: sessionId,
    roster_version: 8, armed: false, estop: false, selection: [11], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_ground_runtime', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'ground_velocity'],
    pending: null, accepted_plan: null, drones: [ground],
  })
  const pose = parseRelayServerEvent({
    v: 1, type: 'observation', event_id: 'root-ground-pose', session: sessionId, device_id: 11, connection_epoch: 1,
    source_id: 'ohmni-pose', node_type: 'ground', frame: 'world', confidence: 0.9, t_capture: null,
    t_source_receipt: { clock_id: 'ohmni-ms', unit: 'ms', value: relayNow }, clock_mapping_id: null,
    payload: { kind: 'pose', pose: { parent_frame: 'world', child_frame: 'base_link', x_m: 1, y_m: 2, z_m: 0, qx: 0, qy: 0, qz: 0, qw: 1 } }, t_ingest: relayNow,
  })
  if (!state || !pose) throw new Error('expected canonical ground relay events')
  act(() => { peers.console.emitServer(state); peers.console.emitServer(pose) })
  await screen.findByText('1 of 1 selected')
  await user.click(screen.getByRole('button', { name: 'Ground' }))
  expect(screen.getByRole('button', { name: 'Forward · 80 mm/s · 250 ms' })).toBeEnabled()
})

test('Devices labels old values as last reported and drops live readiness after silence', () => {
  vi.useFakeTimers()
  let now = relayNow
  const peers = clients(() => now)
  render(<App sessionId={sessionId} clients={peers} initialModule="devices" intentDependencies={{ now: () => now, nextId: () => 'unused' }} />)
  const card = within(screen.getByRole('article', { name: 'D-01 device card' }))
  expect(card.getByText('hovering', { selector: '.dv-state' })).toBeInTheDocument()
  expect(card.getByText('video', { selector: 'dt' }).nextElementSibling).toHaveTextContent('live')
  now += 6_000
  act(() => vi.advanceTimersByTime(1_000))
  expect(card.queryByText('hovering', { exact: true })).not.toBeInTheDocument()
  expect(card.getByText(/last reported hovering/, { selector: '.dv-state' })).toBeInTheDocument()
  expect(card.getByText('video', { selector: 'dt' }).nextElementSibling).toHaveTextContent('unreported')
  expect(card.queryByText('ready', { selector: '.dv-ready' })).not.toBeInTheDocument()
  expect(peers.console.sent).toEqual([])
})
