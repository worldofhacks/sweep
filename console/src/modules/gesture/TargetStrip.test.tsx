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
