import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import { useControlConsole } from '../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, type IntentV1 } from '../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { HttpNavigationClient, type NavigationPreview } from './client'

const session = 'navigation-test'
const now = 1_756_700_000_000

function preview(intent: IntentV1): NavigationPreview {
  return {
    session, intent_id: intent.intent_id, t: now, expires_at_ms: now + 15_000,
    plan: { roster_version: 7, selection: intent.selection, navigation: { route: {
      destination_zone_id: 'lobby', execution_order: [1], routes: [{
        drone: { drone_id: 1 }, arrival_slot: { slot_id: 'lobby-1' },
        waypoints: [{ x_m: 0, y_m: 0, z_m: 1 }, { x_m: 3, y_m: 2, z_m: 1 }],
      }],
    } } },
  }
}

function setup(previewFn = async (intent: IntentV1) => preview(intent)) {
  let time = now
  const clients = {
    console: new FixtureRelayClient(session, () => time, 'console'),
    keyboard: new FixtureRelayClient(session, () => time, 'keyboard'),
  }
  const navigation = { catalog: vi.fn(), preview: vi.fn(previewFn) }
  const { result } = renderHook(() => useControlConsole({ sessionId: session, clients, navigation,
    intentDependencies: { now: () => time, nextId: () => 'route-1' } }))
  const state = (selection = [1]) => clients.console.emitServer({
    v: 1, t: time, type: 'state', event_id: `state-${selection.join('-')}-${time}`, session,
    roster_version: 7, armed: true, estop: false, selection, formation: 'none', spacing: 0.8,
    mode: 'indoor', pending: null, accepted_plan: null, capability_profile: 'c1_basic_control.navigation',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'navigate'], drones: fixtureAircraft(time),
  })
  act(() => state())
  return { result, clients, navigation, state, advance: (ms: number) => { time += ms } }
}

describe('frozen navigation confirmation', () => {
  test('previews through HTTP and sends the same envelope only after confirmation', async () => {
    const { result, clients, navigation } = setup()
    await act(async () => { await result.current.prepareNavigation('lobby') })
    expect(clients.console.sent).toHaveLength(0)
    expect(result.current.pendingRequest?.plan?.route?.plan.navigation.route.destination_zone_id).toBe('lobby')
    expect(result.current.pendingRequest?.plan?.expiresAt).toBe(now + 15_000)
    act(() => { result.current.confirmRequest('route-1') })
    expect(clients.console.sent).toEqual([{ ...navigation.preview.mock.calls[0][0], confirm: true }])
  })

  test.each(['selection', 'disconnect', 'hold'] as const)('discards a delayed preview after %s', async change => {
    let finish: (() => void) | undefined
    const gate = new Promise<void>(resolve => { finish = resolve })
    const { result, clients, state } = setup(async intent => { await gate; return preview(intent) })
    let pending: Promise<IntentV1>
    act(() => { pending = result.current.prepareNavigation('lobby') })
    act(() => {
      if (change === 'selection') state([2])
      else if (change === 'disconnect') clients.console.emitConnection('disconnected')
      else result.current.issueHold()
    })
    await act(async () => {
      finish?.()
      await expect(pending).rejects.toThrow('fleet changed')
    })
    expect(result.current.pendingRequest).toBeNull()
    expect(clients.console.sent.filter(intent => intent.name === 'navigate')).toHaveLength(0)
  })

  test('an expired preview cannot send, and retry needs a fresh preview', async () => {
    const { result, clients, advance } = setup()
    await act(async () => { await result.current.prepareNavigation('lobby') })
    const request = result.current.pendingRequest!
    advance(15_000)
    act(() => { expect(result.current.confirmRequest('route-1')).toBeNull() })
    act(() => { result.current.retryRequest({ ...request, status: 'refused' }) })
    expect(clients.console.sent).toHaveLength(0)
    expect(result.current.pendingRequest).toBeNull()
  })

  test('cancel keeps the preview from sending', async () => {
    const { result, clients } = setup()
    await act(async () => { await result.current.prepareNavigation('lobby') })
    act(() => { result.current.cancelRequest('route-1') })
    act(() => { expect(result.current.confirmRequest('route-1')).toBeNull() })
    expect(clients.console.sent).toHaveLength(0)
  })
})

describe('navigation HTTP contract', () => {
  test('loads the authenticated catalog envelope and validates route identity', async () => {
    const catalog = { floor_id: 'floor-1', catalog_version: 'catalog-1', configuration_id: 'config-1', zones: [{
      zone_id: 'lobby', floor_id: 'floor-1', navigation_allowed: true, arrival_slots: ['lobby-1'], aliases: ['Lobby'],
    }] }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ session, catalog })))
    const client = new HttpNavigationClient({ baseUrl: 'wss://relay.example/ws', token: 'test-token' }, fetcher)
    expect(await client.catalog(session)).toEqual(catalog)
    expect(fetcher).toHaveBeenCalledWith(`https://relay.example/session/${session}/navigation/catalog`,
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer test-token' }) }))
    const intent: IntentV1 = { v: 1, t: now, type: 'intent', intent_id: 'route-1', retry_of: null,
      source: 'console', session, name: 'navigate', args: { zone_id: 'lobby' }, selection: [1], mode: 'indoor', confirm: false }
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify(preview(intent))))
    expect(await client.preview(intent)).toEqual(preview(intent))
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body)).intent.confirm).toBe(true)
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ ...preview(intent), intent_id: 'other-request' })))
    await expect(client.preview(intent)).rejects.toThrow('invalid route preview')
  })

  test('preserves a planner refusal without creating a confirmation', async () => {
    const { result, clients } = setup(async () => { throw new Error('The route crosses blocked space.') })
    await act(async () => { await expect(result.current.prepareNavigation('lobby')).rejects.toThrow('blocked space') })
    await waitFor(() => expect(result.current.pendingRequest).toBeNull())
    expect(clients.console.sent).toHaveLength(0)
  })
})
