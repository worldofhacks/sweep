import { describe, expect, test } from 'vitest'
import type { RelayServerEvent } from '../relay/contract'
import { FixtureRelayClient } from './fixture-relay-client'

describe('explicit development fixture', () => {
  test('does not change roster version when selection changes', async () => {
    const client = new FixtureRelayClient('fixture-session', () => 1_756_700_000_000)
    const states: Extract<RelayServerEvent, { type: 'state' }>[] = []
    client.subscribe((event) => {
      if (event.kind === 'server_event' && event.event.type === 'state') {
        states.push(event.event)
      }
    })
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

    expect(states).toHaveLength(2)
    expect(states.map((state) => state.roster_version)).toEqual([7, 7])
    expect(states[1].selection).toEqual([1, 2])
  })
})
