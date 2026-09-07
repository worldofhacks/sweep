import { act, render, renderHook, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import App from '../App'
import type { RelayAircraftState } from '../relay/contract'
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
