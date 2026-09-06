import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { ControlClients } from '../../control/use-control-console'
import { UnavailableRelayClient, WebSocketRelayClient } from '../../relay/client'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1, type RelayStateEvent } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { createGestureTestRig } from '../../testing/gesture-fixtures'

const session = 'target-strip-session'

interface FixtureClients extends ControlClients {
  console: FixtureRelayClient
  keyboard: FixtureRelayClient
  webcam: FixtureRelayClient
}

function mount() {
  const rig = createGestureTestRig({})
  const wall = () => rig.dependencies.clock.wall()
  const clients: FixtureClients = {
    console: new FixtureRelayClient(session, wall, 'console'),
    keyboard: new FixtureRelayClient(session, wall, 'keyboard'),
    webcam: new FixtureRelayClient(session, wall, 'webcam'),
  }
  let sequence = 0
  render(
    <App
      sessionId={session}
      clients={clients}
      intentDependencies={{ now: wall, nextId: () => `strip-intent-${++sequence}` }}
      initialModule="gesture"
      services={{ gesture: rig.dependencies }}
    />,
  )
  /** A state frame over the fixture roster with the control fields overridden. */
  const emitState = (overrides: { estop?: boolean; selection?: number[] }) =>
    clients.console.emitServer({
      v: 1,
      t: wall(),
      event_id: `strip-state-${++sequence}`,
      state_sequence: sequence,
      type: 'state',
      session,
      roster_version: 7,
      armed: true,
      estop: overrides.estop ?? false,
      selection: overrides.selection ?? [1],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null,
      accepted_plan: null,
      drones: fixtureAircraft(wall(), 4),
    })
  return { clients, emitState }
}

const quick = () => within(screen.getByRole('group', { name: 'Quick commands' }))
const holdButton = () => quick().getByRole('button', { name: 'Hold' })

describe('Target strip quick commands', () => {
  test.each(['unchanged selection', 'roster change', 'desired target degraded', 'desired target unselectable'] as const)(
    'All ready from empty selection handles %s before confirmation through the WebSocket client', async (scenario) => {
    const rig = createGestureTestRig({})
    const wall = () => rig.dependencies.clock.wall()
    const socket = new EventTarget() as EventTarget & { readyState: number; send(payload: string): void; close(): void }
    const sent: string[] = []
    socket.readyState = 1
    socket.send = (payload) => { sent.push(payload) }
    socket.close = () => {}
    const consoleClient = new WebSocketRelayClient(
      { baseUrl: 'ws://relay.example.test', sessionId: session, source: 'console', token: 'test-console-token' },
      { now: wall, createSocket: () => socket as unknown as WebSocket },
    )
    render(
      <App
        sessionId={session}
        clients={{
          console: consoleClient,
          keyboard: new UnavailableRelayClient('Test has no keyboard connection.', wall),
          webcam: new UnavailableRelayClient('Test has no webcam connection.', wall),
        }}
        intentDependencies={{ now: wall, nextId: () => 'fresh-selection-intent' }}
        initialModule="gesture"
        services={{ gesture: rig.dependencies }}
      />,
    )
    const message = (payload: object) => socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(payload) }))
    const initialState: RelayStateEvent = {
      v: 1, t: wall(), event_id: 'fresh-state', state_sequence: 1, type: 'state', session, roster_version: 7,
      armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8, mode: 'indoor',
      capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null, accepted_plan: null, drones: fixtureAircraft(wall(), 4),
    }
    act(() => {
      socket.dispatchEvent(new Event('open'))
      message({ v: 1, t: wall(), event_id: 'fresh-auth', type: 'auth.accepted', session, source: 'console', drone_id: null })
      message(initialState)
    })
    const target = screen.getByRole('group', { name: 'Target' })
    expect(target).toHaveTextContent('0 of 4 selected')
    const user = userEvent.setup()
    await user.click(within(target).getByRole('button', { name: 'All ready' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    const draft = JSON.parse(dock.querySelector('pre')!.textContent!)
    expect(draft).toMatchObject({
      intent_id: 'fresh-selection-intent', name: 'select', source: 'console',
      args: { ids: [1, 2, 4] }, selection: [1, 2, 4], confirm: false,
    })
    expect(isConsoleIntentV1(draft)).toBe(true)
    expect(sent.map((frame) => JSON.parse(frame).type)).toEqual(['auth'])
    expect(target).toHaveTextContent('0 of 4 selected')

    // The relay continues publishing its old selection until this proposal is confirmed.
    for (const stateSequence of [2, 3]) {
      act(() => message({ ...initialState, event_id: `repeat-state-${stateSequence}`, state_sequence: stateSequence }))
      expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBe(dock)
      expect(JSON.parse(dock.querySelector('pre')!.textContent!)).toEqual(draft)
      expect(sent).toHaveLength(1)
    }
    if (scenario !== 'unchanged selection') {
      const changedState: RelayStateEvent = {
        ...initialState, event_id: 'changed-state', state_sequence: 4,
        ...(scenario === 'roster change'
          ? { roster_version: 8 }
          : { drones: initialState.drones.map((drone) => drone.drone_id !== 2 ? drone : {
            ...drone,
            ...(scenario === 'desired target degraded' ? { membership: 'degraded' as const } : { selectable: false }),
          }) }),
      }
      act(() => message(changedState))
      expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
      expect(screen.getByText('Preview invalidated, nothing sent').closest('[role="alert"]')).toHaveTextContent(
        scenario === 'roster change' ? 'stale_roster' : 'stale_selection',
      )
      expect(sent).toHaveLength(1)
      return
    }
    await user.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    expect(sent).toHaveLength(2)
    const intent = JSON.parse(sent[1])
    expect(intent).toEqual({ ...draft, t: wall(), confirm: true })
    expect(isConsoleIntentV1(intent)).toBe(true)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
  })

  test('lists the four quick commands in the design order, each wired through the control hook', async () => {
    mount()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    expect(quick().getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Takeoffconfirm',
      'Holdconfirm',
      'Come home',
      'Land allconfirm',
    ])
    const takeoff = quick().getByRole('button', { name: 'Takeoff' })
    expect(takeoff).toBeEnabled()
    expect(takeoff).not.toHaveClass('is-blocked')
    expect(takeoff).toHaveAttribute(
      'title',
      'Drafts a takeoff preview for D-01; nothing is sent until the dock confirms it.',
    )
    const comeHome = quick().getByRole('button', { name: 'Come home' })
    expect(comeHome).toBeEnabled()
    expect(comeHome).toHaveAttribute(
      'title',
      "Sends come_home to D-01 at once; the relay's answer is recorded under Requests.",
    )
    const landAll = quick().getByRole('button', { name: 'Land all' })
    expect(landAll).toBeEnabled()
    expect(landAll).toHaveAttribute(
      'title',
      'Drafts a land_all preview for D-01, D-02, D-03, D-04; nothing is sent until the dock confirms it. It targets every aircraft in the roster.',
    )
    expect(holdButton()).toBeEnabled()
    expect(holdButton()).not.toHaveClass('is-blocked')
    expect(holdButton()).toHaveAttribute(
      'title',
      'Drafts a hold preview for D-01; nothing is sent until the dock confirms it.',
    )
  })

  test('takeoff and land all park in the dock; come home sends at once', async () => {
    const { clients } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    await user.click(quick().getByRole('button', { name: 'Takeoff' }))
    const takeoffDock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(takeoffDock).getByText('Takeoff', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
    await user.click(within(takeoffDock).getByRole('button', { name: 'Confirm and send' }))
    expect(clients.console.sent).toHaveLength(1)
    expect(clients.console.sent[0]).toMatchObject({
      intent_id: 'strip-intent-1',
      name: 'takeoff',
      source: 'console',
      selection: [1],
      confirm: true,
    })
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    await user.click(quick().getByRole('button', { name: 'Come home' }))
    expect(clients.console.sent).toHaveLength(2)
    expect(clients.console.sent[1]).toMatchObject({
      intent_id: 'strip-intent-2',
      name: 'come_home',
      source: 'console',
      selection: [1],
      confirm: false,
    })
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    await user.click(quick().getByRole('button', { name: 'Land all' }))
    const landDock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(landDock).getByText('Land all fleet', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(2)
    await user.click(within(landDock).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(2)
    expect(clients.webcam.sent).toHaveLength(0)
  })

  test('hold drafts a preview through the dock and sends only on confirm', async () => {
    const { clients } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    await user.click(holdButton())
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText('hold', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(dock).toHaveTextContent('source console')
    expect(clients.console.sent).toHaveLength(0)
    expect(holdButton()).toBeDisabled()
    expect(holdButton()).toHaveAttribute('title', 'Confirm or cancel the pending preview first.')

    await user.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    expect(clients.console.sent).toHaveLength(1)
    expect(clients.console.sent[0]).toMatchObject({
      intent_id: 'strip-intent-1',
      name: 'hold',
      source: 'console',
      selection: [1],
      confirm: true,
    })
    expect(clients.webcam.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(holdButton()).toBeEnabled()
  })

  test('hold is blocked by a dropped console connection, an active stop, and an empty selection', async () => {
    const { clients, emitState } = mount()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    act(() => clients.console.emitConnection('disconnected', 'Relay socket closed.'))
    expect(holdButton()).toBeDisabled()
    expect(holdButton()).toHaveClass('is-blocked')
    expect(holdButton()).toHaveAttribute('title', 'The console connection is disconnected. Nothing can be sent.')

    act(() => clients.console.emitConnection('connected'))
    act(() => emitState({ estop: true }))
    expect(holdButton()).toBeDisabled()
    expect(holdButton()).toHaveAttribute(
      'title',
      'The network stop is active. Motion intents are refused until the relay reports it clear.',
    )

    act(() => emitState({ estop: false, selection: [] }))
    expect(holdButton()).toBeDisabled()
    expect(holdButton()).toHaveAttribute('title', 'No aircraft selected.')
    expect(clients.console.sent).toHaveLength(0)
  })
})
