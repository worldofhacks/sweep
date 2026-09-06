import { describe, expect, test } from 'vitest'
import type { RelayClientEvent } from '../relay/client'
import type { RelayServerEvent } from '../relay/contract'
import { FixtureRelayClient, isFixtureScenarioName } from './fixture-relay-client'

const clock = () => 1_756_700_000_000

function record(client: FixtureRelayClient): RelayClientEvent[] {
  const events: RelayClientEvent[] = []
  client.subscribe((event) => events.push(event))
  return events
}

function states(events: RelayClientEvent[]): Extract<RelayServerEvent, { type: 'state' }>[] {
  return events.flatMap((event) =>
    event.kind === 'server_event' && event.event.type === 'state' ? [event.event] : [],
  )
}

describe('explicit development fixture', () => {
  test('provides a deterministic six-drone media and telemetry fixture', () => {
    const client = new FixtureRelayClient('fixture-session', clock, 'console', 6)
    const events = record(client)

    client.start()

    const [state] = states(events)
    expect(state.drones).toHaveLength(6)
    expect(state.drones.map((drone) => drone.drone_id)).toEqual([1, 2, 3, 4, 5, 6])
    expect(state.drones[2]).toMatchObject({
      membership: 'degraded',
      readiness_reasons: ['telemetry_stale', 'camera_not_ready'],
    })
    expect(state.drones[3].video).toBeUndefined()
    expect(state.drones[5].video).toEqual({ status: 'unreported', last_frame_at: null })
  })

  test('refuses names outside the C2 fixture capability set', async () => {
    const client = new FixtureRelayClient('fixture-session', clock)
    const events = record(client)
    client.start()

    await client.sendIntent({
      v: 1,
      t: 1_756_700_000_001,
      type: 'intent',
      intent_id: 'map-area-intent',
      retry_of: null,
      source: 'console',
      session: 'fixture-session',
      name: 'map_area' as never,
      args: {},
      selection: [1],
      mode: 'indoor',
      confirm: false,
    })

    const last = events.at(-1)
    expect(last).toMatchObject({
      kind: 'server_event',
      event: {
        type: 'refusal',
        intent_id: 'map-area-intent',
        status: 'refused',
        source: 'relay',
        reason: 'unsupported',
        detail: 'map_area is outside the C2 fixture capability set',
      },
    })
    expect(states(events)).toHaveLength(1)
  })

  test('does not change roster version when selection changes', async () => {
    const client = new FixtureRelayClient('fixture-session', clock)
    const events = record(client)
    client.start()

    await client.sendIntent({
      v: 1,
      t: 1_756_700_000_001,
      type: 'intent',
      intent_id: 'selection-intent',
      retry_of: null,
      source: 'console',
      session: 'fixture-session',
      name: 'select',
      args: { ids: [1, 2] },
      selection: [1],
      mode: 'indoor',
      confirm: false,
    })

    const stateEvents = states(events)
    expect(stateEvents).toHaveLength(2)
    expect(stateEvents.map((state) => state.roster_version)).toEqual([7, 7])
    expect(stateEvents[1].selection).toEqual([1, 2])
  })
})

describe('design scenarios as relay data', () => {
  test('recognises only the declared scenario names', () => {
    expect(['control', 'pending4', 'six6', 'down'].every(isFixtureScenarioName)).toBe(true)
    expect(isFixtureScenarioName('recovering')).toBe(false)
    expect(isFixtureScenarioName('')).toBe(false)
  })

  test('pending4: four aircraft, a departure before the roster, and a relay-side pending record', () => {
    const client = new FixtureRelayClient('fixture-session', clock, 'console', 'pending4')
    const events = record(client)
    client.start()

    expect(events[0]).toMatchObject({ kind: 'connection', connection: { status: 'connected' } })
    const membership = events.find(
      (event) => event.kind === 'server_event' && event.event.type === 'membership',
    )
    expect(membership).toMatchObject({
      event: {
        action: 'unexpected_loss',
        drone_id: 5,
        connection_epoch: 2,
        roster_version: 8,
        reason: 'adapter_connection_lost',
        t: clock() - 402_000,
      },
    })
    const [state] = states(events)
    expect(state).toMatchObject({ roster_version: 9, formation: 'line', spacing: 1.5, selection: [1] })
    expect(state.drones.map((drone) => [drone.drone_id, drone.membership, drone.selectable])).toEqual([
      [1, 'ready', true],
      [2, 'ready', true],
      [3, 'degraded', false],
      [4, 'registered', false],
    ])
    expect(state.drones[3]).toMatchObject({
      connection_epoch: 5,
      control_authority: false,
      rc_safety_operator_present: false,
      readiness_reasons: ['control_authority_missing', 'rc_safety_operator_missing'],
    })
    expect(state.drones[2].video).toEqual({ status: 'unreported', last_frame_at: null })
    expect(state.pending).toMatchObject({ name: 'capture_room', args: { capture_id: 'cap-0147' } })
  })

  test('six6: a degraded console link, six aircraft, roster 12', () => {
    const client = new FixtureRelayClient('fixture-session', clock, 'console', 'six6')
    const events = record(client)
    client.start()

    expect(events[0]).toMatchObject({
      kind: 'connection',
      connection: { status: 'degraded', reason: 'A frame arrived that could not be parsed and was dropped.' },
    })
    const [state] = states(events)
    expect(state.roster_version).toBe(12)
    expect(state.drones.map((drone) => drone.membership)).toEqual([
      'ready',
      'ready',
      'degraded',
      'registered',
      'disconnected',
      'leaving',
    ])
    expect(state.drones[4]).toMatchObject({ battery: 0, link: 0, pos_quality: 0 })
    expect(state.pending).toBeNull()

    const keyboard = new FixtureRelayClient('fixture-session', clock, 'keyboard', 'six6')
    const keyboardEvents = record(keyboard)
    keyboard.start()
    expect(keyboardEvents[0]).toMatchObject({ kind: 'connection', connection: { status: 'connected' } })
  })

  test('down: both sources disconnected, no authentication, no state, sends refused', async () => {
    const client = new FixtureRelayClient('fixture-session', clock, 'console', 'down')
    const events = record(client)
    client.start()

    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      kind: 'connection',
      connection: { status: 'disconnected', reason: /Physical RC remains primary/ },
    })
    await expect(
      client.sendIntent({
        v: 1,
        t: clock(),
        type: 'intent',
        intent_id: 'down-intent',
        retry_of: null,
        source: 'console',
        session: 'fixture-session',
        name: 'hold',
        args: {},
        selection: [1],
        mode: 'indoor',
        confirm: false,
      }),
    ).rejects.toThrow('Fixture relay is disconnected')
    expect(client.sent).toHaveLength(1)
  })
})
