import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../App'
import { UnavailableRelayClient } from '../relay/client'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { formatTime } from './format'

const session = 'shell-test-session'
const clock = () => 1_756_700_000_000

type User = ReturnType<typeof userEvent.setup>

function fixtureClients() {
  return {
    console: new FixtureRelayClient(session, clock, 'console'),
    keyboard: new FixtureRelayClient(session, clock, 'keyboard'),
  }
}

function modulesRail() {
  return within(screen.getByRole('navigation', { name: 'Modules' }))
}

async function openModule(user: User, label: string) {
  await user.click(modulesRail().getByRole('button', { name: label }))
}

async function openControlPane(user: User, label: string) {
  const tabs = within(screen.getByRole('group', { name: 'Control panes' }))
  await user.click(tabs.getByRole('button', { name: label }))
}

async function draftCapture(user: User) {
  await openControlPane(user, 'Capture')
  await user.click(screen.getByRole('button', { name: /Capture room/ }))
  return screen.getByRole('region', { name: 'Pending confirmation' })
}

function emitEstop(client: FixtureRelayClient, estop: boolean, eventId: string) {
  client.emitServer({
    v: 1,
    t: clock() + 1,
    type: 'state',
    event_id: eventId,
    session,
    roster_version: 7,
    armed: true,
    estop,
    selection: [1],
    formation: 'none',
    spacing: 0.8,
    mode: 'indoor',
    pending: null,
    accepted_plan: null,
    drones: fixtureAircraft(clock()),
  })
}

describe('persistent shell', () => {
  test('connected and quiet: stop enabled, tags, selection, RC line, link pills, no dock', async () => {
    const clients = fixtureClients()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    const stop = screen.getByRole('button', { name: 'Network stop' })
    expect(stop).toBeEnabled()
    expect(stop).toHaveTextContent('Network stop')
    expect(stop).toHaveTextContent('estop · Shift+Escape')
    expect(stop).toHaveAccessibleDescription('Sends estop to every aircraft in the roster.')

    const tags = within(screen.getByRole('list', { name: 'Session state' }))
    expect(tags.getByText('Armed')).toBeInTheDocument()
    expect(tags.getByText('Stop clear')).toBeInTheDocument()
    expect(tags.getByText('indoor')).toBeInTheDocument()
    expect(tags.getByText('D-01')).toBeInTheDocument()
    expect(tags.getByText('3 of 4 ready')).toBeInTheDocument()
    expect(screen.getByText('D-01 Sweep · RC operator present')).toBeInTheDocument()

    const links = within(screen.getByRole('list', { name: 'Connections' }))
    expect(links.getByTitle('Relay (console)')).toHaveTextContent(/^relay\s*connected$/)
    expect(links.getByTitle('Keyboard stop')).toHaveTextContent(/^keys\s*connected$/)
    expect(links.queryByTitle('Webcam')).not.toBeInTheDocument()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(modulesRail().getByRole('button', { name: 'Control' })).toHaveAttribute('aria-current', 'page')
    expect(modulesRail().getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Control',
      'Live',
      'Gesture',
      'Speech',
      'Captures',
      'Worlds',
      'Reference',
    ])
    expect(within(screen.getByRole('navigation', { name: 'Primary' })).getAllByRole('button')).toHaveLength(7)
  })

  test('pending confirmation: the dock shows the plan with its Intent v1 JSON expanded by default', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(
      <App
        sessionId={session}
        clients={clients}
        intentDependencies={{ now: clock, nextId: () => 'shell-pending-intent' }}
      />,
    )
    await screen.findByText(/Development fixture active/i)

    const dock = await draftCapture(user)
    expect(dock).toHaveFocus()
    expect(dock).toHaveTextContent('Pending — nothing sent')
    expect(dock).toHaveTextContent('D-01 · pano_360')
    expect(dock).toHaveTextContent('roster v7')
    expect(within(dock).getAllByRole('listitem')).toHaveLength(4)

    const toggle = within(dock).getByRole('button', { name: 'Hide Intent v1 envelope' })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const json = within(dock).getByText(/"intent_id": "shell-pending-intent"/)
    expect(json.tagName).toBe('PRE')
    expect(json).toHaveTextContent('"name": "capture_room"')
    expect(json).toHaveTextContent('"confirm": false')

    await user.click(toggle)
    expect(within(dock).getByRole('button', { name: 'Show Intent v1 envelope' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    expect(within(dock).queryByText(/"intent_id"/)).not.toBeInTheDocument()

    await user.click(within(dock).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('module switching never drops a pending request', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(
      <App
        sessionId={session}
        clients={clients}
        intentDependencies={{ now: clock, nextId: () => 'survives-switch' }}
      />,
    )
    await screen.findByText(/Development fixture active/i)
    await draftCapture(user)

    await openModule(user, 'Live')
    expect(screen.getByRole('region', { name: 'Wall of 4' })).toBeInTheDocument()
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText(/"intent_id": "survives-switch"/)).toBeInTheDocument()

    await openModule(user, 'Reference')
    await openModule(user, 'Control')
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ intent_id: 'survives-switch', confirm: true })
  })

  test('invalidation shows its reason in the footer until the next draft', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)
    await draftCapture(user)

    await user.click(screen.getByRole('radio', { name: /Reconstruct 8/ }))
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    const alert = screen.getByText(/Preview invalidated, nothing sent/).closest('[role="alert"]')
    expect(alert).toHaveTextContent('capture_pattern_changed')
    expect(alert).toHaveTextContent('Capture pattern changed. Build and confirm a new preview.')
    expect(clients.console.sent).toHaveLength(0)
  })

  test('disconnected: stop disabled with the relay reason, pills and sheet say so', async () => {
    const clients = {
      console: new UnavailableRelayClient('Console relay missing.', clock),
      keyboard: new UnavailableRelayClient('Keyboard relay missing.', clock),
    }
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)

    const stop = await screen.findByRole('button', { name: 'Network stop' })
    expect(stop).toBeDisabled()
    expect(stop).toHaveAccessibleDescription(
      'Disabled: the console socket is disconnected. Console relay missing. Use the physical RC or Shift+Escape on the keyboard connection.',
    )
    const links = within(screen.getByRole('list', { name: 'Connections' }))
    expect(links.getByTitle('Relay (console)')).toHaveTextContent(/^relay\s*disconnected$/)
    expect(links.getByTitle('Keyboard stop')).toHaveTextContent(/^keys\s*disconnected$/)
    expect(screen.getByText('no aircraft reported')).toBeInTheDocument()
    expect(screen.getByText('0 of 0 ready')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Session detail' }))
    const sheet = within(screen.getByLabelText('Session detail'))
    expect(sheet.getByText(/Disabled: the console socket is disconnected/)).toBeInTheDocument()
    expect(sheet.getByText('Notices — 2 danger · 0 warning · 0 info')).toBeInTheDocument()
    expect(sheet.getByText('operating_state unreported')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide detail' })).toHaveAttribute('aria-expanded', 'true')
  })

  test('warning notices stay visible and announced without opening the session sheet', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    const line = screen.getByRole('status', { name: 'Latest notice' })
    expect(line).toHaveAttribute('aria-live', 'polite')
    expect(line).toBeEmptyDOMElement()

    clients.console.emitConnection('degraded', 'Heartbeat late by 4 s.')
    await waitFor(() =>
      expect(line).toHaveTextContent('Warning — Relay degraded: Heartbeat late by 4 s.'),
    )
    expect(screen.getByRole('status', { name: 'Latest notice' })).toBe(line)
    expect(screen.queryByLabelText('Session detail')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    clients.keyboard.emitConnection('disconnected', 'Keyboard socket closed.')
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Danger — Keyboard stop unavailable: Keyboard socket closed.',
    )
    expect(line).toHaveTextContent('Relay degraded')
    expect(line).not.toHaveTextContent('Keyboard socket closed')

    await user.click(screen.getByRole('button', { name: 'Session detail' }))
    const sheet = within(screen.getByLabelText('Session detail'))
    expect(sheet.getByText('Notices — 1 danger · 1 warning · 0 info')).toBeInTheDocument()
  })

  test('stop disabled reason follows a live socket loss', async () => {
    const clients = fixtureClients()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    clients.console.emitConnection('disconnected', 'Relay socket closed (code 1006). No retry was attempted.')

    const stop = await screen.findByRole('button', { name: 'Network stop' })
    await waitFor(() => expect(stop).toBeDisabled())
    expect(stop).toHaveAccessibleDescription(
      /Disabled: the console socket is disconnected\. Relay socket closed \(code 1006\)\. No retry was attempted\./,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Relay socket closed (code 1006)')
  })

  test('stop active: the button turns danger, stays pressable, and re-sends estop', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={{ now: clock, nextId: () => 'stop-again' }} />)
    await screen.findByText(/Development fixture active/i)

    emitEstop(clients.console, true, 'state-estop-true')

    const stop = await screen.findByRole('button', { name: 'Network stop' })
    await waitFor(() => expect(stop).toHaveTextContent('Stop active'))
    expect(stop).toHaveClass('is-active')
    expect(stop).toBeEnabled()
    expect(stop).toHaveTextContent(`seen ${formatTime(clock())} · Shift+Escape`)
    expect(stop).toHaveAccessibleDescription(/Pressing again re-sends estop/)
    const tags = within(screen.getByRole('list', { name: 'Session state' }))
    expect(tags.getByText('Stop active')).toHaveClass('is-stop-active')

    await user.click(stop)
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ name: 'estop', source: 'console', selection: [] })

    fireEvent.keyDown(window, { key: 'Escape', shiftKey: true })
    await waitFor(() => expect(clients.keyboard.sent).toHaveLength(1))
    expect(clients.keyboard.sent[0]).toMatchObject({ name: 'estop', source: 'keyboard' })
    expect(clients.console.sent).toHaveLength(1)

    emitEstop(clients.console, false, 'state-estop-false')
    await waitFor(() => expect(stop).toHaveTextContent('Network stop'))
    expect(stop).toHaveAccessibleDescription(
      `Stop cleared, seen ${formatTime(clock())}, reported by the relay.`,
    )
    expect(tags.getByText('Stop clear')).toBeInTheDocument()
  })

  test('the Reference module shows its section tabs and an honest empty state', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} />)
    await screen.findByText(/Development fixture active/i)

    await openModule(user, 'Reference')
    const tabs = within(screen.getByRole('group', { name: 'Reference sections' }))
    expect(tabs.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Mission',
      'Health',
      'Config',
      'Ledger',
      'Map',
      'States',
    ])
    await user.click(tabs.getByRole('button', { name: 'Health' }))
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Reference')
    expect(screen.getByText(/Connectivity and health — Nodes, services, metrics/)).toBeInTheDocument()
    const empty = screen.getByText(/does not report shared-service status/)
    expect(empty.closest('[role="status"]')).toHaveTextContent('Nothing to show')
  })
})
