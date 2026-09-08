import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import App from '../App'
import type { NavigationCatalog, NavigationConfirmationOutcome, NavigationPreview, NavigationPreviewRequest } from '../navigation'
import { HttpNavigationClient } from '../platform/navigation-client'
import { PlatformHttp, type PlatformFetch } from '../platform/http'
import { C1_BASIC_CONTROL_INTENTS, type DeviceClass, type RelayAircraftState } from '../relay/contract'
import { fixtureAircraft, FixtureRelayClient } from '../testing/fixture-relay-client'
import { useControlConsole } from './use-control-console'

const NOW = 1_756_700_000_000
const SESSION = 'platform-navigation-ui'

function acceptedCatalog(): NavigationCatalog {
  return {
    session: SESSION, catalogVersion: 'catalog-fixture-1', receivedAt: NOW, expiresAt: NOW + 15_000,
    map: {
      mapId: 'map-fixture', floorId: 'level-1', frame: 'world', accepted: true, approvalId: 'approval-fixture-1',
      mapPin: { version: 'world-fixture-1', contentSha256: 'a'.repeat(64) },
      geometryPin: { version: 'static-fixture-1', contentSha256: 'b'.repeat(64) },
      navigationPin: { version: 'catalog-fixture-1', contentSha256: 'c'.repeat(64) },
    },
    configVersion: 'measured-fixture-1', motionConfig: { fixtureOnly: { maximumGroundSpeedMps: 0.2 } },
    destinations: [{ zoneId: 'lobby', name: 'Main lobby', aliases: ['Reception'], floorId: 'level-1',
      excluded: false, reachability: 'unknown', allowedClasses: ['aircraft', 'ground_vehicle'] }],
  }
}

function refusedPreview(request: NavigationPreviewRequest): NavigationPreview {
  return {
    previewId: 'review-fixture-1', session: SESSION, intentId: request.intentId,
    rosterVersion: request.rosterVersion, selected: request.selected,
    destination: acceptedCatalog().destinations[0], map: request.map, catalogVersion: request.catalogVersion,
    configVersion: request.configVersion, motionConfig: request.motionConfig,
    routes: [], outcomes: request.selected.map((target) => ({ target, status: 'refused',
      code: 'navigation_capability_disabled', detail: 'The current C1 release does not enable navigation execution.' })),
    receivedAt: NOW, expiresAt: NOW + 10_000, dispatchEligible: false,
  }
}

function device(kind: DeviceClass): RelayAircraftState {
  const id = kind === 'aircraft' ? 1 : 11
  return {
    ...fixtureAircraft(NOW)[0], drone_id: id, device_class: kind, unit: 1,
    connection_epoch: kind === 'aircraft' ? 3 : 4,
    flight_state: kind === 'aircraft' ? 'hovering' : 'idle',
    last_seen_at: NOW, telemetry: { t: NOW, fresh: true },
  }
}

type ConfirmationRequest = { previewId: string; intentId: string; previewHash: string }
function confirmation(request: ConfirmationRequest, status: 'refused' | 'invalidated' = 'refused'): NavigationConfirmationOutcome {
  return { previewId: request.previewId, intentId: request.intentId, status,
    code: status === 'refused' ? 'navigation_execution_unavailable' : 'review_binding_changed',
    detail: status === 'refused' ? 'This release cannot execute navigation.' : 'The relay invalidated this frozen review.',
    dispatchEligible: false }
}
function json(value: unknown) { return new Response(JSON.stringify(value)) }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function environment(options: { now?: () => number; catalog?: () => NavigationCatalog;
  confirm?: (request: ConfirmationRequest) => Response | Promise<Response> } = {}) {
  let next = 0
  const dependencies = { now: options.now ?? (() => NOW), nextId: () => `intent-fixture-${++next}` }
  const clients = {
    console: new FixtureRelayClient(SESSION, dependencies.now, 'console'),
    keyboard: new FixtureRelayClient(SESSION, dependencies.now, 'keyboard'),
  }
  const fetcher = vi.fn<PlatformFetch>(async (input, init) => {
    const path = new URL(String(input)).pathname
    if (path.endsWith('/navigation/catalog')) return json({ status: 'ready', catalog: options.catalog?.() ?? acceptedCatalog(), serverNowMs: NOW })
    if (path.endsWith('/navigation/preview')) {
      const request = JSON.parse(init!.body as string) as NavigationPreviewRequest
      return new Response(JSON.stringify({ preview: refusedPreview(request), previewHash: 'd'.repeat(64), serverNowMs: NOW }))
    }
    if (path.endsWith('/navigation/confirm')) {
      const request = JSON.parse(init!.body as string) as ConfirmationRequest
      return options.confirm?.(request) ?? json(confirmation(request))
    }
    throw new Error(`Unexpected test HTTP operation: ${path}`)
  })
  const navigation = new HttpNavigationClient(new PlatformHttp({ baseUrl: 'http://relay.test', sessionId: SESSION, token: 'isolated-test-token' }, fetcher), dependencies.now)
  return { dependencies, clients, fetcher, navigation }
}

function emitReady(env: ReturnType<typeof environment>, classes: DeviceClass[], changes: { grounded?: boolean; estop?: boolean } = {}) {
  const devices = classes.map(device).map((item) => changes.grounded && item.device_class === 'aircraft' ? { ...item, flight_state: 'grounded' as const } : item)
  env.clients.console.emitServer({
    v: 1, type: 'state', event_id: `state-fixture-${classes.join('-')}`, t: NOW, session: SESSION,
    roster_version: 9, armed: true, estop: changes.estop ?? false, selection: devices.map((item) => item.drone_id),
    formation: 'none', spacing: 0.8, mode: 'indoor', pending: null, accepted_plan: null,
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS], drones: devices,
  })
}

async function settle() {
  await act(async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve() })
}

describe('real HTTP destination reviews in the C1 console', () => {
  afterEach(() => { vi.useRealTimers() })
  test.each([
    ['aircraft', ['aircraft']],
    ['ground', ['ground_vehicle']],
    ['mixed', ['aircraft', 'ground_vehicle']],
  ] as const)('renders frozen per-node backend refusals for ready %s selections without enabling motion', async (_label, classes) => {
    const env = environment()
    render(<App sessionId={SESSION} clients={env.clients} intentDependencies={env.dependencies} services={{ navigation: env.navigation }} />)
    act(() => emitReady(env, [...classes]))
    await settle()
    fireEvent.click(within(screen.getByRole('group', { name: 'Control panes' })).getByRole('button', { name: 'Navigate' }))
    const pane = screen.getByRole('region', { name: 'Named-zone navigation' })
    expect(pane).toHaveTextContent('reachability unreported')
    fireEvent.change(within(pane).getByRole('searchbox', { name: 'Destination name or alias' }), { target: { value: 'Reception' } })
    const review = within(pane).getByRole('button', { name: 'Review destination' })
    expect(review).toBeEnabled()
    fireEvent.click(review)
    await settle()

    const requests = env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/preview'))
    expect(requests).toHaveLength(1)
    const body = JSON.parse(requests[0][1]!.body as string) as NavigationPreviewRequest
    expect(body).toMatchObject({ session: SESSION, zoneId: 'lobby', rosterVersion: 9,
      catalogVersion: 'catalog-fixture-1', configVersion: 'measured-fixture-1' })
    expect(body.selected).toEqual(classes.map((kind) => ({ id: kind === 'aircraft' ? 1 : 11, deviceClass: kind, epoch: kind === 'aircraft' ? 3 : 4 })))
    const frozen = env.navigation.getSnapshot().preview
    expect(frozen?.dispatchEligible).toBe(false)
    expect(frozen?.routes).toEqual([])
    expect(Object.isFrozen(frozen?.selected)).toBe(true)
    expect(Object.isFrozen(frozen?.motionConfig.fixtureOnly)).toBe(true)
    expect(env.navigation.getSnapshot().reviewSupported).toBe(true)

    const details = within(pane).getByRole('region', { name: 'Navigation planner preview' })
    expect(details).toHaveTextContent('Main lobby')
    expect(details).toHaveTextContent('static-fixture-1')
    expect(details).toHaveTextContent('approval-fixture-1')
    expect(details).toHaveTextContent('world')
    expect(within(details).getAllByText(/Refused · navigation_capability_disabled/)).toHaveLength(classes.length)
    for (const target of body.selected) expect(details).toHaveTextContent(`ID ${target.id} · epoch ${target.epoch}`)
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    const confirm = within(dock).getByRole('button', { name: 'Confirm and send' })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(C1_BASIC_CONTROL_INTENTS).not.toContain('navigate')
    expect(env.clients.console.sent).toEqual([])
    expect(env.clients.keyboard.sent).toEqual([])
    expect(env.fetcher.mock.calls.map(([input]) => new URL(String(input)).pathname)).toEqual([
      `/api/sessions/${SESSION}/navigation/catalog`, `/api/sessions/${SESSION}/navigation/preview`,
    ])
    expect(details).toHaveTextContent('no takeoff, capture, survey, or formation action')
  })

  test('keeps generic navigate submission blocked while the separate HTTP review provider is available', async () => {
    const env = environment()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env, ['aircraft', 'ground_vehicle']))
    await settle()
    expect(result.current.state.capabilityProfile).toBe('c1_basic_control')
    expect(result.current.state.enabledIntentNames).toEqual(C1_BASIC_CONTROL_INTENTS)
    expect(result.current.navigation.reviewSupported).toBe(true)
    act(() => {
      expect(result.current.issueIntent({ name: 'navigate', args: { zone_id: 'lobby' } })).toBeNull()
      expect(result.current.prepareIntent({ name: 'navigate', args: { zone_id: 'lobby' } })).toBeNull()
    })
    expect(env.clients.console.sent).toEqual([])
    expect(env.fetcher.mock.calls.every(([input]) => String(input).endsWith('/navigation/catalog'))).toBe(true)
  })

  test('retains the unavailable-service refusal and C1 capability gate when the provider is absent', async () => {
    const env = environment()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients, intentDependencies: env.dependencies }))
    act(() => emitReady(env, ['aircraft', 'ground_vehicle']))
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).toBeNull() })
    expect(result.current.navigation.status).toBe('unavailable')
    expect(result.current.navigation.reviewSupported).toBe(false)
    expect(result.current.navigation.reason).toMatch(/no accepted-map catalog/)
    expect(result.current.pendingRequest).toBeNull()
    expect(result.current.state.enabledIntentNames).not.toContain('navigate')
    expect(env.fetcher).not.toHaveBeenCalled()
    expect(env.clients.console.sent).toEqual([])
  })

  test.each(['grounded', 'estop'] as const)('still refuses destination review for %s aircraft despite HTTP review support', async (block) => {
    const env = environment()
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env, ['aircraft'], { [block]: true }))
    await settle()
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).toBeNull() })
    expect(result.current.pendingRequest).toBeNull()
    expect(env.fetcher.mock.calls.every(([input]) => String(input).endsWith('/navigation/catalog'))).toBe(true)
    expect(env.clients.console.sent).toEqual([])
  })

  test.each(['refused', 'invalidated'] as const)('shows the typed %s receipt after retiring the pending request, with no motion send', async (status) => {
    const env = environment({ confirm: (request) => json(confirmation(request, status)) })
    render(<App sessionId={SESSION} clients={env.clients} intentDependencies={env.dependencies} services={{ navigation: env.navigation }} />)
    act(() => emitReady(env, ['aircraft', 'ground_vehicle']))
    await settle()
    fireEvent.click(within(screen.getByRole('group', { name: 'Control panes' })).getByRole('button', { name: 'Navigate' }))
    const pane = screen.getByRole('region', { name: 'Named-zone navigation' })
    fireEvent.change(within(pane).getByRole('searchbox', { name: 'Destination name or alias' }), { target: { value: 'Reception' } })
    fireEvent.click(within(pane).getByRole('button', { name: 'Review destination' }))
    await settle()
    const verify = within(pane).getByRole('button', { name: 'Verify frozen review' })
    expect(verify).toBeEnabled()
    fireEvent.click(verify)
    await settle()
    const requests = env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/confirm'))
    expect(requests).toHaveLength(1)
    const body = JSON.parse(requests[0][1]!.body as string) as ConfirmationRequest
    expect(body).toEqual({ previewId: 'review-fixture-1', intentId: 'intent-fixture-1', previewHash: 'd'.repeat(64) })
    const receipt = within(pane).getByRole('status', { name: 'Frozen review verification' })
    expect(receipt).toHaveTextContent(confirmation(body, status).code)
    expect(receipt).toHaveTextContent(confirmation(body, status).detail)
    expect(receipt).toHaveTextContent(body.previewId)
    expect(receipt).toHaveTextContent(body.intentId)
    expect(receipt).toHaveTextContent('No navigation motion was sent')
    expect(within(pane).getByRole('region', { name: 'Navigation planner preview' })).toHaveTextContent('approval-fixture-1')
    expect(within(pane).getByRole('button', { name: 'Verify frozen review' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Confirm and send' })).not.toBeInTheDocument()
    fireEvent.click(verify)
    await settle()
    expect(env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/confirm'))).toHaveLength(1)
    expect(env.clients.console.sent).toEqual([])
    expect(env.clients.keyboard.sent).toEqual([])
  })

  test('consumes a frozen review only once even when verification is invoked twice before its response', async () => {
    const response = deferred<Response>()
    const env = environment({ confirm: () => response.promise })
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env, ['aircraft', 'ground_vehicle']))
    await settle()
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).not.toBeNull() })
    let first!: Promise<NavigationConfirmationOutcome | null>
    let second!: Promise<NavigationConfirmationOutcome | null>
    act(() => { first = result.current.verifyNavigationReview(); second = result.current.verifyNavigationReview() })
    expect(result.current.navigationVerification.status).toBe('verifying')
    expect(result.current.canVerifyNavigation).toBe(false)
    const request = env.fetcher.mock.calls.find(([input]) => String(input).endsWith('/navigation/confirm'))!
    const expected = confirmation(JSON.parse(request[1]!.body as string) as ConfirmationRequest)
    await act(async () => { response.resolve(json(expected)); expect(await first).toEqual(expected); expect(await second).toBeNull() })
    expect(result.current.navigationVerification).toEqual({ status: 'complete', reason: null, outcome: expected })
    expect(result.current.pendingRequest).toBeNull()
    expect(result.current.navigation.preview?.previewId).toBe(expected.previewId)
    await act(async () => { expect(await result.current.verifyNavigationReview()).toBeNull() })
    expect(env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/confirm'))).toHaveLength(1)
    expect(env.clients.console.sent).toEqual([])
  })

  test.each(['selection', 'map', 'config', 'provider', 'cancel', 'unmount'] as const)('discards a late confirmation after %s changes', async (change) => {
    vi.useFakeTimers()
    let catalog = acceptedCatalog()
    const response = deferred<Response>()
    const env = environment({ catalog: () => catalog, confirm: () => response.promise })
    const replacement = environment()
    const { result, rerender, unmount } = renderHook(({ navigation }) => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation }), { initialProps: { navigation: env.navigation } })
    act(() => emitReady(env, ['aircraft', 'ground_vehicle']))
    await settle()
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).not.toBeNull() })
    let pending!: Promise<NavigationConfirmationOutcome | null>
    act(() => { pending = result.current.verifyNavigationReview() })
    const request = env.fetcher.mock.calls.find(([input]) => String(input).endsWith('/navigation/confirm'))!
    const expected = confirmation(JSON.parse(request[1]!.body as string) as ConfirmationRequest)
    if (change === 'selection') act(() => emitReady(env, ['aircraft']))
    if (change === 'provider') rerender({ navigation: replacement.navigation })
    if (change === 'cancel') act(() => result.current.cancelRequest(expected.intentId))
    if (change === 'unmount') unmount()
    if (change === 'map' || change === 'config') {
      catalog = change === 'map' ? { ...catalog, map: { ...catalog.map, approvalId: 'approval-fixture-2',
        mapPin: { ...catalog.map.mapPin, contentSha256: 'e'.repeat(64) } } }
        : { ...catalog, motionConfig: { fixtureOnly: { maximumGroundSpeedMps: 0.1 } } }
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    }
    await settle()
    await act(async () => { response.resolve(json(expected)); expect(await pending).toBeNull() })
    if (change !== 'unmount') {
      expect(result.current.navigationVerification.status).toBe('invalidated')
      expect(result.current.navigationVerification.outcome).toBeNull()
      expect(result.current.canVerifyNavigation).toBe(false)
    }
    expect(env.clients.console.sent).toEqual([])
    expect(env.clients.keyboard.sent).toEqual([])
    expect(replacement.fetcher.mock.calls.every(([input]) => String(input).endsWith('/navigation/catalog'))).toBe(true)
  })

  test.each(['identity', 'dispatch', 'status', 'extra-field'] as const)('rejects a %s receipt and requires a fresh review', async (fault) => {
    const env = environment({ confirm: (request) => json({ ...confirmation(request),
      ...(fault === 'identity' ? { intentId: 'another-intent' } : fault === 'dispatch' ? { dispatchEligible: true }
        : fault === 'status' ? { status: 'accepted' } : { unexpected: 'untrusted evidence' }) }) })
    const { result } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env, ['aircraft']))
    await settle()
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).not.toBeNull() })
    await act(async () => { expect(await result.current.verifyNavigationReview()).toBeNull() })
    expect(result.current.navigationVerification.status).toBe('error')
    expect(result.current.navigationVerification.outcome).toBeNull()
    expect(result.current.navigationVerification.reason).toMatch(/new destination review/)
    expect(result.current.canVerifyNavigation).toBe(false)
    await act(async () => { expect(await result.current.verifyNavigationReview()).toBeNull() })
    expect(env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/confirm'))).toHaveLength(1)
    expect(env.clients.console.sent).toEqual([])
  })

  test('does not consume an expired review', async () => {
    let time = NOW
    const env = environment({ now: () => time })
    const { result, rerender } = renderHook(() => useControlConsole({ sessionId: SESSION, clients: env.clients,
      intentDependencies: env.dependencies, navigation: env.navigation }))
    act(() => emitReady(env, ['aircraft']))
    await settle()
    await act(async () => { expect(await result.current.prepareNavigation('lobby')).not.toBeNull() })
    time = NOW + 10_000
    rerender()
    await act(async () => { expect(await result.current.verifyNavigationReview()).toBeNull() })
    expect(result.current.canVerifyNavigation).toBe(false)
    expect(env.fetcher.mock.calls.filter(([input]) => String(input).endsWith('/navigation/confirm'))).toHaveLength(0)
    expect(env.clients.console.sent).toEqual([])
  })
})
