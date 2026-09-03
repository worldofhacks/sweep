import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from './App'
import { UnavailableRelayClient } from './relay/client'
import type { IntentV1 } from './relay/contract'
import { FixtureRelayClient, fixtureAircraft } from './testing/fixture-relay-client'

const session = 'component-test-session'
const clock = () => 1_756_700_000_000

type User = ReturnType<typeof userEvent.setup>

function fixtureClients() {
  return {
    console: new FixtureRelayClient(session, clock, 'console'),
    keyboard: new FixtureRelayClient(session, clock, 'keyboard'),
  }
}

async function openModule(user: User, label: string) {
  const rail = within(screen.getByRole('navigation', { name: 'Modules' }))
  await user.click(rail.getByRole('button', { name: label }))
}

async function openControlPane(user: User, label: string) {
  const tabs = within(screen.getByRole('group', { name: 'Control panes' }))
  await user.click(tabs.getByRole('button', { name: label }))
}

async function openLivePane(user: User, label: string) {
  const tabs = within(screen.getByRole('group', { name: 'Live panes' }))
  await user.click(tabs.getByRole('button', { name: label }))
}

class FailingFixtureRelayClient extends FixtureRelayClient {
  override async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    throw new Error('Socket closed before the intent frame was written.')
  }
}

class DelayedSelectionFixtureRelayClient extends FixtureRelayClient {
  override async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    await new Promise(() => undefined)
  }
}

describe('Control / Capture console', () => {
  test('renders a four-source fixture mosaic and keeps media focus local to the console', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByText('1 of 4 selected')).toBeInTheDocument()

    await openModule(user, 'Live')
    expect(screen.getByRole('region', { name: 'Wall of 4' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Focus D-/ })).toHaveLength(4)
    expect(screen.getByRole('button', { name: 'Focus D-01' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Focus D-02' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getAllByText(/adapter reports the stream offline/)).not.toHaveLength(0)
    expect(screen.getAllByText(/No video reported/)).not.toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Focus D-02' }))
    expect(screen.getByRole('button', { name: 'Focus D-02' })).toHaveAttribute('aria-pressed', 'true')
    await openLivePane(user, 'Focus feed')
    expect(screen.getByRole('region', { name: 'Focused aircraft D-02' })).toBeInTheDocument()

    await openModule(user, 'Control')
    expect(screen.getByText('1 of 4 selected')).toBeInTheDocument()
    await openModule(user, 'Live')
    expect(screen.getByRole('button', { name: 'Focus D-02' })).toHaveAttribute('aria-pressed', 'true')
    await openLivePane(user, 'Focus feed')
    expect(screen.getByRole('region', { name: 'Focused aircraft D-02' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('renders and focuses every source in the six-drone fixture', async () => {
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 6),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 6),
    }
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)
    await openModule(user, 'Live')
    await openLivePane(user, 'Wall of 6')

    expect(await screen.findAllByRole('button', { name: /Focus D-/ })).toHaveLength(6)
    await user.click(screen.getByRole('button', { name: 'Focus D-06' }))
    expect(screen.getByRole('button', { name: 'Focus D-06' })).toHaveAttribute('aria-pressed', 'true')

    await openLivePane(user, 'Focus feed')
    expect(screen.getByRole('region', { name: 'Focused aircraft D-06' })).toHaveTextContent(
      'unreported',
    )
    expect(clients.console.sent).toHaveLength(0)
  })

  test('renders an authoritative last frame age for an unreported source', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    const lastFrameAt = clock() - 4_000
    render(
      <App
        sessionId={session}
        clients={clients}
        intentDependencies={{ now: clock, nextId: () => 'last-frame-intent' }}
      />,
    )
    await screen.findByText(/Development fixture active/i)

    const drones = fixtureAircraft(clock())
    drones[3] = { ...drones[3], video: { status: 'unreported', last_frame_at: lastFrameAt } }
    clients.console.emitServer({
      v: 1,
      t: clock() + 1,
      type: 'state',
      event_id: 'state-unreported-last-frame',
      session,
      roster_version: 7,
      armed: true,
      estop: false,
      selection: [1],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      drones,
    })

    await openModule(user, 'Live')
    await user.click(screen.getByRole('button', { name: 'Focus D-04' }))
    await openLivePane(user, 'Focus feed')
    expect(screen.getByRole('region', { name: 'Focused aircraft D-04' })).toHaveTextContent('4 s ago')
  })

  test('is honestly disconnected when no production relay bootstrap exists', async () => {
    const clients = {
      console: new UnavailableRelayClient('Console relay missing.', clock),
      keyboard: new UnavailableRelayClient('Keyboard relay missing.', clock),
    }
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Danger — /)
    expect(screen.getByText('0 of 0 selected')).toBeInTheDocument()
    const stop = screen.getByRole('button', { name: 'Network stop' })
    expect(stop).toBeDisabled()
    expect(stop).toHaveAccessibleDescription(
      /Disabled: the console socket is disconnected\. Console relay missing\./,
    )
    expect(screen.queryByText(/simulator active/i)).not.toBeInTheDocument()
  })

  test('previews capture before sending and confirms the same Intent v1 ID', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByText(/Development fixture active/i)).toBeInTheDocument()
    await openControlPane(user, 'Capture')
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    const sent = clients.console.sent[0]
    expect(sent).toMatchObject({
      name: 'capture_room',
      source: 'console',
      selection: [1],
      confirm: true,
      args: { room_id: 'room-01', pattern: 'pano_360' },
    })
    expect((sent.args as { capture_id: string }).capture_id).toContain(sent.intent_id)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    await openControlPane(user, 'Requests')
    expect(screen.getAllByText('Accepted by the explicit development fixture.')).not.toHaveLength(0)
  })

  test('refreshes the wire timestamp after a delayed capture confirmation without changing the intent', async () => {
    let currentTime = 1_756_700_000_000
    const now = () => currentTime
    const clients = {
      console: new FixtureRelayClient(session, now, 'console'),
      keyboard: new FixtureRelayClient(session, now, 'keyboard'),
    }
    const user = userEvent.setup()
    render(
      <App
        sessionId={session}
        clients={clients}
        intentDependencies={{ now, nextId: () => 'delayed-capture-intent' }}
      />,
    )
    await screen.findByText(/Development fixture active/i)

    await openControlPane(user, 'Capture')
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText(/"intent_id": "delayed-capture-intent"/)).toBeInTheDocument()
    currentTime += 30_000
    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))

    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      t: currentTime,
      intent_id: 'delayed-capture-intent',
      confirm: true,
      selection: [1],
      args: {
        room_id: 'room-01',
        capture_id: 'capture-delayed-capture-intent',
        pattern: 'pano_360',
      },
    })
  })

  test('routes Shift+Escape only through the separately authenticated keyboard producer', async () => {
    const clients = fixtureClients()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    fireEvent.keyDown(window, { key: 'Escape', shiftKey: true })

    await waitFor(() => expect(clients.keyboard.sent).toHaveLength(1))
    expect(clients.keyboard.sent[0]).toMatchObject({
      name: 'estop',
      source: 'keyboard',
      selection: [],
    })
    expect(clients.console.sent).toHaveLength(0)
  })

  test('surfaces keyboard safety actions when the console connection is unavailable', async () => {
    const keyboard = new FixtureRelayClient(session, clock, 'keyboard')
    render(
      <App
        sessionId={session}
        clients={{
          console: new UnavailableRelayClient('Console relay missing.', clock),
          keyboard,
        }}
      />,
    )
    expect(await screen.findAllByText('Console relay missing.')).not.toHaveLength(0)

    keyboard.emitServer({
      v: 1,
      t: clock(),
      type: 'safety_action',
      event_id: 'keyboard-safety-action',
      session,
      drone_id: 1,
      connection_epoch: 3,
      reason: 'link_loss',
      action: 'failsafe',
      loss_behavior: 'failsafe',
    })
    keyboard.emitServer({
      v: 1,
      t: clock() + 1,
      type: 'state',
      event_id: 'keyboard-state-after-estop',
      session,
      roster_version: 7,
      armed: true,
      estop: true,
      selection: [1],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      drones: fixtureAircraft(clock()),
    })

    expect(await screen.findByText('Aircraft failsafe')).toBeInTheDocument()
    expect(await screen.findByText('Network stop active')).toBeInTheDocument()
  })

  test('runs the two-drone flight workflow through production control actions', async () => {
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 4, false),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 4, false),
    }
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    await openControlPane(user, 'Fleet')
    await user.click(screen.getByRole('button', { name: 'Select D-02' }))
    await openControlPane(user, 'Swarm')
    await user.click(screen.getByRole('button', { name: 'Arm' }))
    await user.click(screen.getByRole('button', { name: /^Takeoff/i }))
    expect(clients.console.sent.map((intent) => intent.name)).toEqual(['select', 'arm'])
    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))

    await user.click(screen.getByRole('button', { name: 'Translate north' }))
    await user.click(screen.getByRole('button', { name: 'Hold' }))
    await user.click(screen.getByRole('button', { name: /^Come home/ }))
    await user.click(screen.getByRole('button', { name: /^Land all/ }))
    expect(clients.console.sent.map((intent) => intent.name)).toEqual([
      'select',
      'arm',
      'takeoff',
      'translate',
      'hold',
      'come_home',
    ])
    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))

    expect(clients.console.sent.map((intent) => intent.name)).toEqual([
      'select',
      'arm',
      'takeoff',
      'translate',
      'hold',
      'come_home',
      'land_all',
    ])
    expect(clients.console.sent.find((intent) => intent.name === 'takeoff')).toMatchObject({
      selection: [1, 2],
      confirm: true,
    })
    expect(clients.console.sent.find((intent) => intent.name === 'translate')).toMatchObject({
      args: { dx: 0, dy: 2 },
      selection: [1, 2],
    })
    expect(clients.console.sent.find((intent) => intent.name === 'land_all')).toMatchObject({
      selection: [],
      confirm: true,
    })
  })

  test('invalidates a confirmed-flight preview when authoritative selection changes', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: /^Takeoff/i }))
    await openControlPane(user, 'Fleet')
    await user.click(screen.getByRole('button', { name: 'Select D-02' }))
    await openControlPane(user, 'Swarm')

    expect(clients.console.sent.map((intent) => intent.name)).toEqual(['select'])
    expect(screen.queryByRole('button', { name: 'Confirm and send' })).not.toBeInTheDocument()
    expect(await screen.findAllByText('selection_change_requested')).not.toHaveLength(0)
  })

  test('invalidates a preview as soon as a new selection is requested', async () => {
    const clients = {
      console: new DelayedSelectionFixtureRelayClient(session, clock, 'console'),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard'),
    }
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: /^Takeoff/i }))
    await openControlPane(user, 'Fleet')
    await user.click(screen.getByRole('button', { name: 'Select D-02' }))
    await openControlPane(user, 'Swarm')

    expect(clients.console.sent.map((intent) => intent.name)).toEqual(['select'])
    expect(screen.queryByRole('button', { name: 'Confirm and send' })).not.toBeInTheDocument()
    expect(await screen.findAllByText('selection_change_requested')).not.toHaveLength(0)
  })

  test('shows send failure and does not retry or substitute a command', async () => {
    const consoleClient = new FailingFixtureRelayClient(session, clock, 'console')
    const keyboardClient = new FixtureRelayClient(session, clock, 'keyboard')
    const user = userEvent.setup()
    render(
      <App
        sessionId={session}
        clients={{ console: consoleClient, keyboard: keyboardClient }}
      />,
    )
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: 'Hold' }))

    expect(
      await screen.findAllByText('Socket closed before the intent frame was written.'),
    ).not.toHaveLength(0)
    expect(consoleClient.sent).toHaveLength(1)
    expect(consoleClient.sent[0].name).toBe('hold')
  })

  test('blocks an unsupported pattern without silently changing it', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: /^D-02 / }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ name: 'select', args: { ids: [2] } })
    expect(screen.getByRole('button', { name: /^D-02 / })).toHaveAttribute('aria-pressed', 'true')

    await openControlPane(user, 'Capture')
    expect(await screen.findByText(/D-02 does not advertise pano_360/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Capture room/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /^pano_360/ })).toHaveAttribute('aria-pressed', 'true')
  })
})
