import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from './App'
import { UnavailableRelayClient } from './relay/client'
import type { IntentV1 } from './relay/contract'
import { FixtureRelayClient } from './testing/fixture-relay-client'

const session = 'component-test-session'
const clock = () => 1_756_700_000_000

function fixtureClients() {
  return {
    console: new FixtureRelayClient(session, clock, 'console'),
    keyboard: new FixtureRelayClient(session, clock, 'keyboard'),
  }
}

class FailingFixtureRelayClient extends FixtureRelayClient {
  override async sendIntent(intent: IntentV1): Promise<void> {
    this.sent.push(intent)
    throw new Error('Socket closed before the intent frame was written.')
  }
}

describe('Control / Capture console', () => {
  test('starts live playback for the selected relay source and tears it down', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    const close = vi.fn().mockResolvedValue(undefined)
    const start = vi.fn(async (
      _video: HTMLVideoElement,
      _descriptor: unknown,
      onState: (state: string) => void,
    ) => onState('playing_whep'))

    const view = render(
      <App
        sessionId={session}
        clients={clients}
        mediaConfiguration={{
          webrtcOrigin: 'http://localhost:8889',
          hlsOrigin: 'http://localhost:8888',
          readerUsername: 'reader',
          readerPassword: 'secret',
        }}
        createMediaSession={() => ({ start, close })}
      />,
    )

    await screen.findByText(/Development fixture active/i)
    await user.click(screen.getAllByRole('button', { name: 'View feed' })[0])
    const video = await screen.findByLabelText('Live feed for D-01')
    await waitFor(() => expect(start).toHaveBeenCalled())
    expect(video).toBeInstanceOf(HTMLVideoElement)
    expect(screen.getByText('Media playback playing_whep')).toBeInTheDocument()

    view.unmount()
    expect(close).toHaveBeenCalled()
  })

  test('is honestly disconnected when no production relay bootstrap exists', async () => {
    const clients = {
      console: new UnavailableRelayClient('Console relay missing.', clock),
      keyboard: new UnavailableRelayClient('Keyboard relay missing.', clock),
    }
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Network controls unavailable')
    expect(screen.getByText('No aircraft state')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Network E-stop' })).toBeDisabled()
    expect(screen.queryByText(/simulator active/i)).not.toBeInTheDocument()
  })

  test('previews capture before sending and confirms the same Intent v1 ID', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)

    expect(await screen.findByText(/Development fixture active/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    expect(screen.getByRole('heading', { name: 'Plan request preview' })).toBeInTheDocument()
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

    expect(await screen.findByText(/D-02 does not report pano_360/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Capture room/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /Pano 360/ })).toHaveAttribute('aria-checked', 'true')
  })
})
