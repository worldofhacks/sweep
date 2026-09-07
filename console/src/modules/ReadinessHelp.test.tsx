import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import App from '../App'
import { C1_BASIC_CONTROL_INTENTS, SUPERVISED_VERTICAL_INTENTS } from '../relay/contract'
import { FixtureRelayClient, fixtureAircraft, fixtureScenario } from '../testing/fixture-relay-client'
import { readinessNotes } from '../shell/readiness'

test('two-node setup guidance distinguishes operator permission from positioning and preserves relay selection gates', async () => {
  const session = 'readiness-help'
  const now = () => 1_756_700_000_000
  const clients = {
    console: new FixtureRelayClient(session, now, 'console'),
    keyboard: new FixtureRelayClient(session, now, 'keyboard'),
  }
  const user = userEvent.setup()
  render(<App sessionId={session} clients={clients} intentDependencies={{ now, nextId: () => 'unused' }} initialModule="gesture" />)
  await screen.findByText('1 of 4 selected')
  const drones = fixtureAircraft(now()).slice(0, 2).map((drone) => ({
    ...drone, pos_quality: 0, flight_state: 'landed', battery: 0.74, link: 1,
    ...(drone.drone_id === 2 ? {
      membership: 'degraded' as const, selectable: false,
      control_authority: false, rc_safety_operator_present: false, home_pose: null,
      readiness_reasons: ['home_pose_missing', 'control_authority_missing', 'rc_safety_operator_missing'],
    } : {}),
  }))
  act(() => clients.console.emitServer({
    v: 1, t: now(), type: 'state', event_id: 'live-two-nodes', state_sequence: 1, session,
    roster_version: 8, armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8,
    mode: 'indoor', capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    pending: null, accepted_plan: null, drones,
  }))

  const first = within(screen.getByRole('article', { name: 'D-01 registry card' }))
  expect(first.getByText('ready')).toHaveClass('tone-warn')
  expect(first.getByText(/Position quality is 0%/)).toHaveClass('tone-warn')
  expect(first.getByRole('button', { name: 'Select D-01' })).toBeEnabled()
  const second = screen.getByRole('article', { name: 'D-02 registry card' })
  expect(second).toHaveTextContent('Home pose confirmed')
  expect(second).toHaveTextContent('Home pose is not confirmed')
  expect(second).toHaveTextContent('While landed, establish real positioning')
  expect(second).toHaveTextContent('Readiness → Control authority')
  expect(second).toHaveTextContent('does not mean Virtual Stick is enabled')
  expect(second).toHaveTextContent('Readiness → RC safety operator present')
  expect(second).toHaveTextContent('a person at the physical RC; a connection alone is insufficient')
  expect(second).toHaveTextContent('Position quality is 0%')
  expect(within(second).getByText('Sweep control not granted')).toHaveClass('tone-danger')
  expect(within(second).getByRole('button', { name: 'Select D-02' })).toBeDisabled()
  expect(screen.queryByText('RC takeover')).not.toBeInTheDocument()

  const target = within(screen.getByRole('group', { name: 'Target' }))
  expect(target.getByText(/D-01, D-02 position quality 0%/)).toBeVisible()
  expect(target.getByRole('button', { name: 'Select D-02' })).toBeDisabled()
  await user.click(target.getByText('Readiness help · D-01, D-02'))
  expect(target.getByLabelText('D-02 readiness help')).toHaveTextContent('Home pose confirmed')
  expect(target.getByLabelText('D-02 readiness help')).toHaveTextContent('Readiness → Control authority')
  expect(target.getByLabelText('D-02 readiness help')).toHaveTextContent('Readiness → RC safety operator present')
  expect(target.getByLabelText('D-02 readiness help')).toHaveTextContent('Position quality is 0%')

  await user.click(within(screen.getByRole('navigation', { name: 'Modules' })).getByRole('button', { name: 'Control' }))
  await user.click(within(screen.getByRole('group', { name: 'Control panes' })).getByRole('button', { name: 'Fleet' }))
  const registry = within(screen.getByRole('region', { name: 'Registry' }))
  const wideFirst = within(registry.getByRole('article', { name: 'D-01 registry card' }))
  expect(wideFirst.getByText('ready')).toHaveClass('tone-warn')
  expect(wideFirst.getByText(/Position quality is 0%/)).toBeVisible()
  const wideSecond = registry.getByRole('article', { name: 'D-02 registry card' })
  expect(wideSecond).toHaveTextContent('Home pose confirmed')
  expect(wideSecond).toHaveTextContent('Readiness → Control authority')
  expect(wideSecond).toHaveTextContent('Readiness → RC safety operator present')
  expect(within(wideSecond).getByRole('button', { name: 'Select D-02' })).toBeDisabled()

  act(() => clients.console.emitServer({
    v: 1, t: now(), type: 'state', event_id: 'supervised-vertical-position-help', state_sequence: 2, session,
    roster_version: 8, armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8,
    mode: 'indoor', capability_profile: 'supervised_vertical', enabled_intent_names: [...SUPERVISED_VERTICAL_INTENTS],
    pending: null, accepted_plan: null, drones,
  }))
  expect(registry.getByRole('article', { name: 'D-01 registry card' })).toHaveTextContent(
    'Position quality is 0%. Supervised takeoff requires fresh onboard height. Mapped flight remains unavailable.',
  )
  expect(registry.getByRole('article', { name: 'D-01 registry card' })).not.toHaveTextContent(
    'Check device positioning; the relay’s quality limit still applies.',
  )
  expect(clients.console.sent).toEqual([])
  expect(clients.keyboard.sent).toEqual([])
})


test('supervised vertical position guidance remains aircraft-specific in a mixed fleet', () => {
  const ground = fixtureScenario('mixed').fleet(1_756_700_000_000).find((drone) => drone.device_class === 'ground_vehicle')
  if (!ground) throw new Error('mixed fixture must include a ground vehicle')

  const notes = readinessNotes({ ...ground, pos_quality: 0 }, 'supervised_vertical')
  expect(notes.map(({ text }) => text)).toContain(
    'Position quality is 0%. Live telemetry does not establish valid positioning. Check device positioning; the relay’s quality limit still applies.',
  )
  expect(notes.map(({ text }) => text).join(' ')).not.toContain('Supervised takeoff requires fresh onboard height')
})
