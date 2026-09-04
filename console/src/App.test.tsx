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

class FailingFixtureRelayClient extends FixtureRelayClient {
  override async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    throw new Error('Socket closed before the intent frame was written.')
  }
}

describe('Control / Capture console', () => {
  test('renders a four-source fixture mosaic and keeps media focus local to the console', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByRole('heading', { name: 'Aircraft registry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View feed D-01' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'View feed D-02' })).toHaveAttribute('aria-pressed', 'false')

    await openModule(user, 'Live')
    expect(screen.getByRole('heading', { name: 'Camera mosaic' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Focus D-/ })).toHaveLength(4)
    expect(screen.getByRole('region', { name: 'Focused camera D-01' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Focus D-01' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Focus D-02' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getAllByText('Video offline')).not.toHaveLength(0)
    expect(screen.getAllByText('Stream unreported')).not.toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'Focus D-02' }))
    expect(screen.getByRole('region', { name: 'Focused camera D-02' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Focus D-02' })).toHaveAttribute('aria-pressed', 'true')

    await openModule(user, 'Control')
    expect(screen.getByRole('button', { name: 'View feed D-02' })).toHaveAttribute('aria-pressed', 'true')
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

    expect(await screen.findAllByRole('button', { name: /Focus D-/ })).toHaveLength(6)
    await user.click(screen.getByRole('button', { name: 'Focus D-06' }))

    expect(screen.getByRole('region', { name: 'Focused camera D-06' })).toHaveTextContent(
      'Stream unreported',
    )
    expect(screen.getByRole('button', { name: 'Focus D-06' })).toHaveAttribute('aria-pressed', 'true')
    expect(clients.console.sent).toHaveLength(0)
  })

  test('renders an authoritative last frame time for an unreported source', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    const lastFrameAt = clock() - 4_000
    const formattedLastFrame = new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(lastFrameAt)
    render(<App sessionId={session} clients={clients} />)
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
    expect(screen.getByRole('region', { name: 'Focused camera D-04' })).toHaveTextContent(
      `Last frame ${formattedLastFrame}`,
    )
  })

  test('is honestly disconnected when no production relay bootstrap exists', async () => {
    const clients = {
      console: new UnavailableRelayClient('Console relay missing.', clock),
      keyboard: new UnavailableRelayClient('Keyboard relay missing.', clock),
    }
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Danger — /)
    expect(screen.getByText('No aircraft state')).toBeInTheDocument()
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
    expect(screen.getByText(/delayed-capture-intent/)).toBeInTheDocument()
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

    await openControlPane(user, 'Capture')
    await user.click(screen.getByRole('button', { name: /Hold selected/ }))

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

    await user.click(screen.getByRole('button', { name: /D-02 Ready epoch 1 Select/i }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: /D-01 Ready epoch 3 Selected/i }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))

    await openControlPane(user, 'Capture')
    expect(await screen.findByText(/D-02 does not report pano_360/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Capture room/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /Pano 360/ })).toHaveAttribute('aria-checked', 'true')
  })
})
