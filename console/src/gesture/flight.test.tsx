import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, test } from 'vitest'
import { useControlConsole } from '../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1, type RelayAircraftState, type RelayStateEvent } from '../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { createGestureTestRig, hand } from '../testing/gesture-fixtures'
import type { GestureCategory } from './policy'
import { useGestureProducer } from './use-gesture-producer'

const session = 'flight-two-node-test'
type Latest = { control: ReturnType<typeof useControlConsole>; producer: ReturnType<typeof useGestureProducer> }

async function mount() {
  const rig = createGestureTestRig()
  const now = rig.dependencies.clock.wall
  let sequence = 0
  const clients = {
    console: new FixtureRelayClient(session, now, 'console'),
    keyboard: new FixtureRelayClient(session, now, 'keyboard'),
    webcam: new FixtureRelayClient(session, now, 'webcam'),
  }
  let latest: Latest
  const intentDependencies = { now, nextId: () => `flight-${++sequence}` }
  function Harness() {
    const control = useControlConsole({ sessionId: session, clients, intentDependencies })
    const producer = useGestureProducer({ control, roomId: '', dependencies: rig.dependencies, profile: 'flight' })
    useEffect(() => { latest = { control, producer } })
    return <video ref={producer.videoRef} />
  }
  render(<Harness />)
  await act(async () => {})
  const drones = (): RelayAircraftState[] => fixtureAircraft(now(), 4).slice(0, 2).map((drone) => ({
    ...drone, flight_state: 'hovering', adapter_capabilities: [...drone.adapter_capabilities, 'body_pulse_v1'],
  }))
  const emitState = (selection = [1], overrides: Partial<RelayStateEvent> = {}) => act(() => clients.console.emitServer({
    v: 1, t: now(), event_id: `flight-state-${++sequence}`, state_sequence: sequence,
    type: 'state', session, roster_version: 7, armed: true, estop: false, selection,
    formation: 'none', spacing: 0.8, mode: 'indoor', capability_profile: 'c1_basic_control',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'body_pulse'], pending: null,
    accepted_plan: null, drones: drones(), ...overrides,
  }))
  emitState()
  const get = () => latest
  const hold = (category: GestureCategory | null, durationMs: number) => {
    for (let elapsed = 0; elapsed < durationMs; elapsed += 50) {
      act(() => rig.frame(category === null ? [] : [hand(category)], 50))
    }
  }
  await act(async () => { await get().producer.enable() })
  return { clients, get, hold, emitState, drones, now }
}

describe('Flight profile with two selected-capable nodes', () => {
  test('one-node forward and two-node backward freeze exact pulse envelopes; cancel emits nothing and retry parks again', async () => {
    const { clients, get, hold, emitState, now } = await mount()
    hold('Victory', 650)
    const forward = get().control.pendingRequest!.intent
    expect(forward).toMatchObject({ name: 'body_pulse', source: 'webcam', confirm: false, selection: [1], args: { forward_mm_s: 250, duration_ms: 500 } })
    expect(clients.webcam.sent).toHaveLength(0)
    hold(null, 250)
    hold('Thumb_Up', 1000)
    expect(clients.webcam.sent).toEqual([{ ...forward, t: clients.webcam.sent[0].t, confirm: true }])

    emitState([1, 2])
    hold(null, 250)
    hold('Closed_Fist', 650)
    const cancelled = get().control.pendingRequest!.intent
    expect(cancelled).toMatchObject({ selection: [1, 2], args: { forward_mm_s: -250, duration_ms: 500 }, confirm: false })
    hold(null, 250)
    hold('Thumb_Down', 450)
    expect(get().control.pendingRequest).toBeNull()
    expect(get().control.state.requests.find((request) => request.intent.intent_id === cancelled.intent_id)?.status).toBe('cancelled')
    expect(clients.webcam.sent).toHaveLength(1)

    hold(null, 250)
    hold('Closed_Fist', 650)
    const backward = get().control.pendingRequest!.intent
    expect(backward.intent_id).not.toBe(cancelled.intent_id)
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam.sent).toHaveLength(2)
    expect(clients.webcam.sent[1]).toMatchObject({ ...backward, confirm: true, t: now() })
    act(() => clients.webcam.emitServer({
      v: 1, t: now(), event_id: 'pulse-refused', type: 'refusal', session, intent_id: backward.intent_id,
      command_id: null, status: 'refused', source: 'relay', reason: 'safety_gate', detail: 'Test refusal',
      roster_version: 7, drone_id: null, connection_epoch: null,
    }))
    act(() => get().control.retryRequest(get().control.state.requests.find((request) => request.intent.intent_id === backward.intent_id)!))
    const retry = get().control.pendingRequest!.intent
    expect(retry).toMatchObject({ retry_of: backward.intent_id, selection: [1, 2], args: backward.args, confirm: false })
    expect(retry.intent_id).not.toBe(backward.intent_id)
    expect(clients.webcam.sent).toHaveLength(2)
    act(() => {
      expect(get().control.confirmRequest(retry.intent_id)).not.toBeNull()
      expect(get().control.confirmRequest(retry.intent_id)).toBeNull()
    })
    expect(clients.webcam.sent).toHaveLength(3)
    expect(clients.webcam.sent[2]).toMatchObject({ ...retry, confirm: true, t: now() })
    expect(clients.console.sent).toHaveLength(0)
    expect(clients.webcam.sent.every(isConsoleIntentV1)).toBe(true)
  })

  test('selection changes invalidate the frozen preview and capability loss refuses confirmation without a send', async () => {
    const { clients, get, hold, emitState, drones } = await mount()
    hold('Victory', 650)
    const oldId = get().control.pendingRequest!.intent.intent_id
    emitState([1, 2])
    expect(get().control.pendingRequest).toBeNull()
    act(() => { expect(get().control.confirmRequest(oldId)).toBeNull() })
    hold(null, 250)
    hold('Victory', 650)
    const bothId = get().control.pendingRequest!.intent.intent_id
    const changed = drones().map((drone) => drone.drone_id === 2 ? { ...drone, adapter_capabilities: ['flight'] } : drone)
    emitState([1, 2], { drones: changed })
    act(() => { expect(get().control.confirmRequest(bothId)).toBeNull() })
    expect(get().control.pendingRequest).toBeNull()
    hold(null, 250)
    hold('Closed_Fist', 650)
    expect(get().producer.view.lastAction).toMatchObject({ kind: 'blocked', detail: 'D-02 does not advertise body_pulse_v1.' })
    expect(clients.webcam.sent).toHaveLength(0)
  })

  test('arm session has no motor targets and can be confirmed with both aircraft selected', async () => {
    const { clients, get, hold, emitState } = await mount()
    emitState([1, 2], { armed: false })
    hold('Open_Palm', 650)
    const draft = get().control.pendingRequest!.intent
    expect(draft).toMatchObject({ name: 'arm', args: {}, selection: [], confirm: false })
    expect(get().control.pendingRequest!.plan?.steps.join(' ')).toContain('does not start any aircraft motors')
    expect(clients.webcam.sent).toHaveLength(0)
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam.sent).toHaveLength(1)
    expect(clients.webcam.sent[0]).toMatchObject({ ...draft, confirm: true, t: clients.webcam.sent[0].t })
  })
})
