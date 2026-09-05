import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { ControlClients } from '../../control/use-control-console'
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
      type: 'state',
      session,
      roster_version: 7,
      armed: true,
      estop: overrides.estop ?? false,
      selection: overrides.selection ?? [1],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      drones: fixtureAircraft(wall(), 4),
    })
  return { clients, emitState }
}

const quick = () => within(screen.getByRole('group', { name: 'Quick commands' }))
const holdButton = () => quick().getByRole('button', { name: 'Hold' })

describe('Target strip quick commands', () => {
  test('lists the four quick commands in the design order; only hold is live on this contract', async () => {
    mount()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    expect(quick().getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Takeoffunsupported',
      'Holdconfirm',
      'Come homeunsupported',
      'Land allunsupported',
    ])
    const takeoff = quick().getByRole('button', { name: 'Takeoff' })
    expect(takeoff).toBeDisabled()
    expect(takeoff).toHaveClass('is-blocked')
    expect(takeoff).toHaveAttribute(
      'title',
      'The relay does not accept takeoff from this console at M2.0; it is listed until the relay accepts it. Confirmation would be required before send.',
    )
    const comeHome = quick().getByRole('button', { name: 'Come home' })
    expect(comeHome).toBeDisabled()
    expect(comeHome).toHaveAttribute(
      'title',
      'The relay does not accept come_home from this console at M2.0; it is listed until the relay accepts it.',
    )
    const landAll = quick().getByRole('button', { name: 'Land all' })
    expect(landAll).toBeDisabled()
    expect(landAll).toHaveAttribute(
      'title',
      'The relay does not accept land_all from this console at M2.0; it is listed until the relay accepts it. Confirmation would be required before send; it targets every aircraft in the roster.',
    )
    expect(holdButton()).toBeEnabled()
    expect(holdButton()).not.toHaveClass('is-blocked')
    expect(holdButton()).toHaveAttribute(
      'title',
      'Drafts a hold preview for D-01; nothing is sent until the dock confirms it.',
    )
  })

  test('hold drafts a preview through the dock and sends only on confirm', async () => {
    const { clients } = mount()
    const user = userEvent.setup()
    await screen.findByText(/Development fixture active/i)
    await act(async () => {})

    await user.click(holdButton())
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('D-01 · hold')
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
