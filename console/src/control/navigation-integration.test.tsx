import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import App from '../App'
import { useControlConsole } from './use-control-console'
import { navigationTargets } from './navigation'
import { createInitialControlState, type ControlState } from './state'
import { parseNavigationCatalog, parseNavigationPreview,
  type NavigationCatalog, type NavigationDestination, type NavigationPreview, type NavigationSnapshot,
  type NavigationClient, type NavigationPreviewRequest } from '../navigation'
import type { DeviceClass, RelayAircraftState } from '../relay/contract'
import { fixtureAircraft, FixtureRelayClient } from '../testing/fixture-relay-client'
const NOW = 1_756_700_000_000
const SESSION = 'navigation-ui-test'

function device(id: number, deviceClass: DeviceClass, unit: number, epoch: number): RelayAircraftState {
  return {
    ...fixtureAircraft(NOW)[0], drone_id: id, device_class: deviceClass, unit,
    connection_epoch: epoch, flight_state: deviceClass === 'aircraft' ? 'hovering' : 'idle',
    last_seen_at: NOW, telemetry: { t: NOW, fresh: true },
  }
}

function state(classes: DeviceClass[] = ['aircraft']): ControlState {
  const devices = classes.map((kind, index) => device(index + 11, kind, index + 3, index + 7))
  return {
    ...createInitialControlState(SESSION, NOW),
    connection: { status: 'connected', transport: 'fixture', changedAt: NOW },
    capabilityProfile: 'navigation-review-test', enabledIntentNames: ['navigate'],
    rosterVersion: 9, aircraft: Object.fromEntries(devices.map((item) => [item.drone_id, item])),
    selection: devices.map((item) => item.drone_id), armed: true,
    lastStateEvent: { rosterVersion: 9, t: NOW, source: 'console', receivedAt: NOW },
  }
}

function destination(zoneId: string, name: string, overrides: Partial<NavigationDestination> = {}): NavigationDestination {
  return { zoneId, name, aliases: [], floorId: 'floor-1', excluded: false, reachability: 'reachable', allowedClasses: ['aircraft', 'ground_vehicle'], ...overrides }
}

function catalog(overrides: Partial<NavigationCatalog> = {}): NavigationCatalog {
  const parsed = parseNavigationCatalog({
    session: SESSION, catalogVersion: 'catalog-7', receivedAt: NOW - 1000, expiresAt: NOW + 60_000,
    map: {
      mapId: 'accepted-building', floorId: 'floor-1', frame: 'world', accepted: true, approvalId: 'approval-3',
      mapPin: { version: 'map-4', contentSha256: 'a'.repeat(64) },
      geometryPin: { version: 'geometry-5', contentSha256: 'b'.repeat(64) },
      navigationPin: { version: 'navigation-6', contentSha256: 'c'.repeat(64) },
    },
    configVersion: 'motion-8', motionConfig: { test_only_measured_ground_speed_m_s: 0.2 },
    destinations: [
      destination('lobby', 'Main lobby', { aliases: ['Reception'] }),
      destination('lab-east', 'East laboratory', { aliases: ['Lab'] }),
      destination('lab-west', 'West laboratory', { aliases: ['Lab'] }),
      destination('excluded', 'Restricted room', { excluded: true }),
      destination('upstairs', 'Upper room', { floorId: 'floor-2' }),
      destination('closed', 'Closed room', { reachability: 'unreachable' }),
      destination('unknown', 'Unassessed room', { reachability: 'unknown' }),
      destination('air-only', 'Air route', { allowedClasses: ['aircraft'] }),
    ], ...overrides,
  })
  if (!parsed) throw new Error('invalid test catalog')
  return parsed
}

function preview(current: ControlState, accepted = catalog(), refusedId?: number): NavigationPreview {
  const selected = navigationTargets(current)
  const parsed = parseNavigationPreview({
    previewId: 'preview-1', intentId: 'intent-1', session: SESSION, rosterVersion: current.rosterVersion,
    selected, destination: accepted.destinations[0], map: accepted.map,
    catalogVersion: accepted.catalogVersion, configVersion: accepted.configVersion, motionConfig: accepted.motionConfig,
    receivedAt: NOW, expiresAt: NOW + 30_000, dispatchEligible: false,
    routes: selected.filter((target) => target.id !== refusedId).map((target, index) => {
      const end = { xM: index + 2, yM: 3, zM: target.deviceClass === 'aircraft' ? 1.5 : 0, floorId: 'floor-1', frame: 'world' }
      return {
        target,
        waypoints: [{ ...end, xM: 0, yM: 0 }, end],
        arrivalSlot: { slotId: `slot-${target.id}`, zoneId: 'lobby', position: end },
        holdBehavior: target.deviceClass === 'aircraft' ? 'hover' : 'stop',
      }
    }),
    outcomes: selected.map((target) => ({
      target, status: target.id === refusedId ? 'refused' : 'planned',
      code: target.id === refusedId ? 'unsupported_for_device_class' : 'route_planned',
      detail: target.id === refusedId ? 'Navigation is not supported by this selected adapter.' : 'Route and arrival slot reported by the planner.',
    })),
  })
  if (!parsed) throw new Error('invalid test preview')
  return parsed
}

function snapshot(accepted = catalog(), planned: NavigationPreview | null = null): NavigationSnapshot {
  return { status: 'ready', reason: null, catalog: accepted, preview: planned }
}

class ReviewClient implements NavigationClient {
  snapshot = snapshot()
  listeners = new Set<(value: NavigationSnapshot) => void>()
  requestPreview = vi.fn(async (request: NavigationPreviewRequest) => ({ ...preview(state(['aircraft', 'ground_vehicle'])), intentId: request.intentId }))
  getSnapshot() { return this.snapshot }
  subscribe(listener: (value: NavigationSnapshot) => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  update(next: NavigationSnapshot) { this.snapshot = next; for (const listener of this.listeners) listener(next) }
}
function setup() {
  let at = NOW
  let seq = 0
  const dependencies = { now: () => at, nextId: () => 'intent-' + ++seq }
  return { dependencies, navigation: new ReviewClient(), advance: (ms: number) => { at += ms },
    clients: { console: new FixtureRelayClient(SESSION, dependencies.now, 'console'),
      keyboard: new FixtureRelayClient(SESSION, dependencies.now, 'keyboard') } }
}
function emitReady(clients: ReturnType<typeof setup>['clients'], selection = [11, 12], grounded = false) {
  const current = state(['aircraft', 'ground_vehicle'])
  clients.console.emitServer({ v: 1, type: 'state', event_id: 'ready-' + selection.join('-') + '-' + grounded,
    t: NOW, session: SESSION, roster_version: 9, armed: true, estop: false, selection,
    formation: 'none', spacing: 0.8, mode: 'indoor', pending: null, accepted_plan: null,
    capability_profile: 'navigation_integration_test', enabled_intent_names: ['navigate', 'hold', 'select'],
    drones: Object.values(current.aircraft).map((item) => grounded ? { ...item, flight_state: 'grounded' } : item) })
}
function deferred() {
  let resolve!: (value: NavigationPreview) => void
  const promise = new Promise<NavigationPreview>((done) => { resolve = done })
  return { promise, resolve }
}

describe('navigation review console lifecycle', () => {
  test('stages frozen mixed-device evidence and blocks every navigation send path', async () => {
    const env = setup()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    const pending = result.current.pendingRequest
    expect(pending?.intent).toMatchObject({ name: 'navigate', args: { zone_id: 'lobby' }, selection: [11, 12], confirm: false })
    expect(pending?.plan?.navigation?.routes.map((route) => route.holdBehavior)).toEqual(['hover', 'stop'])
    expect(Object.isFrozen(pending?.plan?.navigation?.routes)).toBe(true)
    expect(pending?.plan?.confirmationBlockedReason).toMatch(/unavailable/)
    act(() => {
      expect(result.current.issueIntent({ name: 'navigate', args: { zone_id: 'lobby' } })).toBeNull()
      expect(result.current.prepareIntent({ name: 'navigate', args: { zone_id: 'lobby' } })).toBeNull()
      expect(result.current.confirmRequest(pending!.intent.intent_id)).toBeNull()
    })
    expect(env.clients.console.sent).toEqual([])
    expect(env.clients.keyboard.sent).toEqual([])
    expect(result.current.pendingRequest).toBeNull()
  })
  test.each(['selection', 'map', 'destination', 'other-preview', 'expiry'] as const)('drops a late result after %s changes', async (change) => {
    const env = setup(), reply = deferred()
    env.navigation.requestPreview.mockImplementation(() => reply.promise)
    const { result, rerender } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    let work!: Promise<unknown>
    act(() => { work = result.current.prepareNavigation('lobby') })
    expect(env.navigation.requestPreview).toHaveBeenCalledTimes(1)
    act(() => {
      if (change === 'selection') emitReady(env.clients, [11])
      if (change === 'map') env.navigation.update(snapshot(catalog({ catalogVersion: 'catalog-new' })))
      if (change === 'destination') result.current.invalidateNavigation()
      if (change === 'other-preview') result.current.prepareHold('console')
      if (change === 'expiry') { env.advance(35_000); rerender() }
    })
    await act(async () => { reply.resolve(preview(state(['aircraft', 'ground_vehicle']))); await work })
    expect(result.current.pendingRequest?.intent.name).not.toBe('navigate')
    expect(result.current.navigation.status).not.toBe('loading')
    expect(env.clients.console.sent).toEqual([])
  })
  test('invalidates a staged plan when geometry changes', async () => {
    const env = setup()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    expect(result.current.pendingRequest).not.toBeNull()
    act(() => env.navigation.update(snapshot(catalog({ map: { ...catalog().map,
      geometryPin: { version: 'geometry-new', contentSha256: 'd'.repeat(64) } } }))))
    expect(result.current.pendingRequest).toBeNull()
    expect(result.current.navigation.preview).toBeNull()
  })
  test.each(['provider', 'context'] as const)('does not revive a review when invalid %s values are restored', async (change) => {
    const env = setup()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    const frozen = result.current.pendingRequest!.plan!.navigation!
    if (change === 'provider') {
      const changed = { ...frozen, routes: frozen.routes.map((route) => ({ ...route,
        waypoints: route.waypoints.map((point, index) => index === 0 ? { ...point, xM: 0.5 } : point) })) }
      act(() => env.navigation.update(snapshot(catalog(), changed)))
    } else act(() => env.navigation.update(snapshot(catalog({ catalogVersion: 'catalog-new' }))))
    expect(result.current.pendingRequest).toBeNull()
    expect(result.current.navigation.preview).toBeNull()
    act(() => env.navigation.update(snapshot(catalog(), change === 'provider' ? frozen : null)))
    expect(result.current.navigation.preview).toBeNull()
    expect(result.current.pendingRequest).toBeNull()
  })
  test('accepts a matching provider publication before its preview promise resolves', async () => {
    const env = setup(), reply = deferred()
    env.navigation.requestPreview.mockImplementation(() => reply.promise)
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    let work!: Promise<unknown>
    act(() => { work = result.current.prepareNavigation('lobby') })
    const frozen = preview(state(['aircraft', 'ground_vehicle']))
    act(() => env.navigation.update(snapshot(catalog(), frozen)))
    await act(async () => { reply.resolve(frozen); await work })
    expect(result.current.pendingRequest?.intent.name).toBe('navigate')
    expect(result.current.navigation.preview).not.toBeNull()
  })
  test('refuses grounded aircraft and a mismatched response identity', async () => {
    const env = setup()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients, [11, 12], true))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    expect(env.navigation.requestPreview).not.toHaveBeenCalled()
    act(() => emitReady(env.clients))
    env.navigation.requestPreview.mockImplementation(async () => ({ ...preview(state(['aircraft', 'ground_vehicle'])), intentId: 'wrong-intent' }))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    expect(result.current.pendingRequest).toBeNull()
    expect(result.current.navigation.reason).toMatch(/another request/)
  })
  test('keeps current per-device refusal details available for review', async () => {
    const env = setup()
    env.navigation.requestPreview.mockImplementation(async (request) => ({ ...preview(state(['aircraft', 'ground_vehicle']), catalog(), 12), intentId: request.intentId }))
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env.clients))
    await act(async () => { await result.current.prepareNavigation('lobby') })
    expect(result.current.pendingRequest?.plan?.navigation?.outcomes[1].status).toBe('refused')
    expect(env.clients.console.sent).toEqual([])
  })
  test('opens alias review through the existing Control pane and disables dock confirmation', async () => {
    const env = setup()
    render(<App sessionId={SESSION} clients={env.clients} intentDependencies={env.dependencies} services={{ navigation: env.navigation }} />)
    act(() => emitReady(env.clients))
    fireEvent.click(within(screen.getByRole('group', { name: 'Control panes' })).getByRole('button', { name: 'Navigate' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Destination name or alias' }), { target: { value: 'Reception' } })
    fireEvent.click(screen.getByRole('button', { name: /Review destination/ }))
    const dock = await screen.findByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByRole('button', { name: 'Confirm and send' })).toBeDisabled()
    expect(dock).toHaveTextContent('Main lobby')
    expect(dock).toHaveTextContent('geometry-5')
    expect(env.clients.console.sent).toEqual([])
  })
})
