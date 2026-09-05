import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { IntentFactoryDependencies } from '../../control/intent'
import { useControlConsole, type ControlClients } from '../../control/use-control-console'
import { formatTime } from '../../shell/format'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { CapturePane, GuidancePanel } from './CapturePane'
import { MissionTracker } from './MissionTracker'
import { refusalCopy, type CaptureReadiness } from './controls'

const session = 'control-module-test'
const t0 = 1_756_700_000_000

type User = ReturnType<typeof userEvent.setup>

function fixtureClients(now: () => number = () => t0): ControlClients & {
  console: FixtureRelayClient
  keyboard: FixtureRelayClient
} {
  return {
    console: new FixtureRelayClient(session, now, 'console'),
    keyboard: new FixtureRelayClient(session, now, 'keyboard'),
  }
}

function sequentialIds(now: () => number = () => t0): IntentFactoryDependencies {
  let seq = 0
  return { now, nextId: () => `intent-${(seq += 1)}` }
}

async function openPane(user: User, label: string) {
  const tabs = within(screen.getByRole('group', { name: 'Control panes' }))
  await user.click(tabs.getByRole('button', { name: label }))
}

function fleetGroup() {
  return within(screen.getByRole('group', { name: 'Fleet controls' }))
}

function motionGroup() {
  return within(screen.getByRole('group', { name: 'Motion controls' }))
}

async function confirmDock(user: User) {
  const dock = screen.getByRole('region', { name: 'Pending confirmation' })
  await user.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
}

const guidance: CaptureReadiness = {
  guidance_mode: 'visual_advisory',
  pose_source: 'visual_odometry',
  pose_ok: true,
  clearance_ok: true,
  camera_ok: true,
  storage_ok: true,
  motion_ok: true,
  image_quality_ok: true,
  coverage: ['accepted', 'accepted', 'weak', 'unseen', 'unseen', 'accepted', 'weak', 'accepted'],
  next_heading_deg: 135,
  suggested_delta: 'yaw +42°',
}

describe('Control › Swarm: the M2.0 workflow on the fixture client', () => {
  test('arm, select all, takeoff, translate, hold, come home, land all — one intent id per request, confirmed where the rule says', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')

    await user.click(fleetGroup().getByRole('button', { name: 'Arm' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      intent_id: 'intent-1',
      name: 'arm',
      args: {},
      selection: [1],
      confirm: false,
      retry_of: null,
    })

    await user.click(fleetGroup().getByRole('button', { name: 'Select all ready' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))
    expect(clients.console.sent[1]).toMatchObject({ name: 'select', args: { ids: [1, 2, 4] } })
    expect(await screen.findByText('3 of 4 selected')).toBeInTheDocument()

    await user.click(motionGroup().getByRole('button', { name: /^Takeoff/ }))
    expect(clients.console.sent).toHaveLength(2)
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Takeoff')
    expect(dock).toHaveTextContent('D-01 D-02 D-04')
    expect(within(dock).getByText(/"intent_id": "intent-3"/)).toBeInTheDocument()
    expect(screen.getByLabelText('Per-aircraft fan-out')).toHaveTextContent(
      'The planner proposes 3 per-aircraft commands.',
    )
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(3))
    expect(clients.console.sent[2]).toMatchObject({
      intent_id: 'intent-3',
      name: 'takeoff',
      args: {},
      selection: [1, 2, 4],
      confirm: true,
    })
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Translate east' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(4))
    expect(clients.console.sent[3]).toMatchObject({ name: 'translate', args: { dx: 2, dy: 0 }, confirm: false })

    await user.click(motionGroup().getByRole('button', { name: 'Hold' }))
    await user.click(motionGroup().getByRole('button', { name: 'Come home' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(6))
    expect(clients.console.sent[4]).toMatchObject({ name: 'hold', args: {}, selection: [1, 2, 4] })
    expect(clients.console.sent[5]).toMatchObject({ name: 'come_home', args: {}, selection: [1, 2, 4] })

    await user.click(motionGroup().getByRole('button', { name: /^Land all/ }))
    const landDock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(landDock).toHaveTextContent('Land all fleet')
    expect(landDock).toHaveTextContent('D-01 D-02 D-03 D-04')
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(7))
    expect(clients.console.sent[6]).toMatchObject({
      intent_id: 'intent-7',
      name: 'land_all',
      selection: [1, 2, 3, 4],
      confirm: true,
    })
    expect(clients.console.sent.every((intent) => intent.retry_of === null)).toBe(true)
    expect(clients.keyboard.sent).toHaveLength(0)

    await openPane(user, 'Requests')
    const takeoff = screen.getByRole('listitem', { name: 'takeoff accepted' })
    expect(takeoff).toHaveTextContent('intent-3')
    expect(takeoff).toHaveTextContent('D-01 D-02 D-04')
    const stamps = within(takeoff).getByLabelText('Lifecycle timestamps')
    expect(stamps).toHaveTextContent(`draft ${formatTime(t0)}`)
    expect(stamps).toHaveTextContent('pending_confirmation')
    expect(stamps).toHaveTextContent('sent')
    expect(stamps).toHaveTextContent('accepted')
    expect(screen.getByRole('listitem', { name: 'translate accepted' })).toHaveTextContent('source console')
    const landAll = screen.getByRole('listitem', { name: 'land_all accepted' })
    expect(landAll).toHaveTextContent('D-01 D-02 D-03 D-04')
    expect(landAll).toHaveTextContent('Accepted by the explicit development fixture.')
    // Accepted is not terminal, so no outcome card claims a result the relay has not reported.
    expect(screen.queryByLabelText('Latest outcome')).not.toBeInTheDocument()
  })

  test('unsupported controls are greyed with the refusal copy, stay pressable, and the refusal is recorded', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')

    const disarm = fleetGroup().getByRole('button', { name: /^Disarm/ })
    expect(disarm).toBeEnabled()
    expect(disarm).toHaveClass('is-soft', 'is-unsupported')
    expect(disarm).toHaveTextContent('unsupported')
    expect(fleetGroup().getByText(refusalCopy('disarm'))).toBeInTheDocument()
    for (const label of [/^Sweep/, /^Spacing tighter/, /^Spacing wider/, /^Formation next/]) {
      const button = motionGroup().getByRole('button', { name: label })
      expect(button).toBeEnabled()
      expect(button).toHaveClass('is-soft')
    }

    await user.click(disarm)
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ name: 'disarm', args: {} })

    await user.click(motionGroup().getByRole('button', { name: /^Formation next/ }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))
    expect(clients.console.sent[1]).toMatchObject({ name: 'formation_next', args: {} })

    await user.click(motionGroup().getByRole('button', { name: /^Sweep/ }))
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toHaveTextContent('Sweep area')
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(3))
    expect(clients.console.sent[2]).toMatchObject({ name: 'sweep', args: {}, confirm: true })

    await user.click(screen.getByRole('button', { name: 'circle' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(4))
    expect(clients.console.sent[3]).toMatchObject({ name: 'formation_set', args: { name: 'circle' } })
    expect(screen.getByRole('button', { name: 'circle' })).toHaveAttribute('aria-pressed', 'true')
    expect(
      screen.getByText('Previewing circle. The relay still reports none — formation_set is refused as unsupported at M2.0.'),
    ).toBeInTheDocument()

    await openPane(user, 'Requests')
    for (const name of ['disarm', 'formation_next', 'sweep', 'formation_set']) {
      const row = screen.getByRole('listitem', { name: `${name} refused` })
      expect(row).toHaveTextContent('unsupported')
      expect(row).toHaveTextContent('The relay does not accept this intent at this milestone.')
      expect(row).toHaveTextContent(`${name} is outside the M2.0 capability set`)
      expect(within(row).getByLabelText('Lifecycle timestamps')).toHaveTextContent('refused')
    }
    expect(screen.getByLabelText('Latest outcome')).toHaveTextContent('formation_set refused')
  })

  test('retry mints a new id with retry_of; a confirmation-gated retry re-enters the preview', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')

    await user.click(fleetGroup().getByRole('button', { name: /^Disarm/ }))
    await user.click(motionGroup().getByRole('button', { name: /^Sweep/ }))
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))

    await openPane(user, 'Requests')
    const disarmRow = screen.getByRole('listitem', { name: 'disarm refused' })
    await user.click(within(disarmRow).getByRole('button', { name: 'Retry as new intent' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(3))
    expect(clients.console.sent[2]).toMatchObject({
      intent_id: 'intent-3',
      retry_of: 'intent-1',
      name: 'disarm',
      args: {},
      selection: [1],
    })
    const retried = await screen.findAllByRole('listitem', { name: 'disarm refused' })
    expect(retried[0]).toHaveTextContent('Retry of')
    expect(retried[0]).toHaveTextContent('intent-1')

    const sweepRow = screen.getByRole('listitem', { name: 'sweep refused' })
    expect(within(sweepRow).getByText(/opens a new preview to confirm/)).toBeInTheDocument()
    await user.click(within(sweepRow).getByRole('button', { name: 'Retry as new intent' }))
    expect(clients.console.sent).toHaveLength(3)
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText(/"retry_of": "intent-2"/)).toBeInTheDocument()
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(4))
    expect(clients.console.sent[3]).toMatchObject({ intent_id: 'intent-4', retry_of: 'intent-2', name: 'sweep', confirm: true })
  })

  test('the translate pad and the motion controls are disabled with a stated reason when nothing is selected', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')

    clients.console.emitServer({
      v: 1,
      t: t0 + 1,
      type: 'state',
      event_id: 'state-nothing-selected',
      session,
      roster_version: 7,
      armed: true,
      estop: false,
      selection: [],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      drones: fixtureAircraft(t0),
    })
    expect(await screen.findByText('0 of 4 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Translate north' })).toBeDisabled()
    expect(screen.getAllByText('No aircraft selected.').length).toBeGreaterThan(0)
    expect(motionGroup().getByRole('button', { name: /^Takeoff/ })).toBeDisabled()
    expect(motionGroup().getByRole('button', { name: /^Sweep/ })).toBeDisabled()
    expect(fleetGroup().getByRole('button', { name: 'Arm' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /^D-04 / }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ name: 'select', args: { ids: [4] } })
    expect(await screen.findByText('1 of 4 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Translate north' })).toBeEnabled()
  })
})

describe('Control › Capture', () => {
  test('validates the room identifier inline and invalidates a pending capture when it changes', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')
    await openPane(user, 'Capture')

    const room = screen.getByLabelText('Room identifier')
    expect(room).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByText('Valid. The capture id is minted from the intent id at draft time.')).toBeInTheDocument()
    expect(screen.getByText('D-01 selected, hovering and ready')).toBeInTheDocument()
    expect(screen.getByText('gates unreported')).toBeInTheDocument()
    expect(screen.getByText('guidance unreported')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Capture room' }))
    const detail = screen.getByRole('region', { name: 'Plan detail' })
    expect(detail).toHaveTextContent('Capture room')
    expect(within(detail).getByText(/"capture_id": "capture-intent-1"/)).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Hide Intent v1 envelope' })).toHaveAttribute('aria-expanded', 'true')

    await user.type(room, 'x')
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    const alert = screen.getByText(/Preview invalidated, nothing sent/).closest('[role="alert"]')
    expect(alert).toHaveTextContent('configuration_changed')
    expect(clients.console.sent).toHaveLength(0)

    await user.clear(room)
    await user.type(room, 'Kitchen')
    expect(room).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Lower-case letters, digits and hyphens, 3 to 24 characters.')).toBeInTheDocument()
    expect(screen.getByText('room identifier needed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Capture room' })).toBeDisabled()

    await user.clear(room)
    await user.type(room, 'kitchen-01')
    expect(room).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByText('kitchen-01 · pano_360')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Capture room' }))
    await confirmDock(user)
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      name: 'capture_room',
      args: { room_id: 'kitchen-01', capture_id: 'capture-intent-2', pattern: 'pano_360' },
      selection: [1],
      confirm: true,
    })
  })

  test('the compass and gates render visual_advisory, registered_metric, and the unreported state', () => {
    const { rerender } = render(<GuidancePanel guidance={guidance} />)
    expect(screen.getByText('guidance visual_advisory')).toBeInTheDocument()
    expect(screen.getByText('pose visual_odometry')).toBeInTheDocument()
    expect(screen.getByText('135°')).toBeInTheDocument()
    expect(screen.getByText('2 unseen, 2 weak, 4 accepted')).toBeInTheDocument()
    expect(screen.getByText('yaw +42°')).toBeInTheDocument()
    expect(screen.getByText(/No XYZ move is suggested in this mode/)).toBeInTheDocument()
    const gates = within(screen.getByRole('list', { name: 'Readiness gates' }))
    expect(gates.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'posepass',
      'clearancepass',
      'camerapass',
      'storagepass',
      'motionpass',
      'image_qualitypass',
    ])

    rerender(<GuidancePanel guidance={{ ...guidance, guidance_mode: 'registered_metric', storage_ok: false, next_heading_deg: 90, suggested_delta: '+0.4 m north' }} />)
    expect(screen.getByText('guidance registered_metric')).toBeInTheDocument()
    expect(screen.getByText('registered_metric: metric moves are available.')).toBeInTheDocument()
    expect(screen.getByText('90°')).toBeInTheDocument()
    expect(screen.getByText('+0.4 m north')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Readiness gates' })).getByText('fail')).toHaveClass('tone-danger')

    rerender(<GuidancePanel guidance={null} />)
    expect(screen.getByText('guidance unreported')).toBeInTheDocument()
    expect(screen.getByText('pose unreported')).toBeInTheDocument()
    expect(screen.getByText('coverage unreported')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Readiness gates' })).getAllByText('unreported')).toHaveLength(6)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  test('a failing motion gate blocks Capture room and names the gate; passing gates enable it', async () => {
    const clients = fixtureClients()
    function Harness({ readiness }: { readiness: CaptureReadiness }) {
      const controller = useControlConsole({ sessionId: session, clients, intentDependencies: sequentialIds() })
      return <CapturePane controller={controller} roomId="room-01" onRoomId={() => {}} guidance={readiness} />
    }
    const { rerender } = render(<Harness readiness={{ ...guidance, motion_ok: false }} />)
    await screen.findByText('D-01 selected, hovering and ready')
    expect(screen.getByText('gates blocking: motion')).toHaveClass('tone-danger')
    expect(screen.getByText('The motion gate fails: the aircraft is still moving. Hold it, then capture.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Capture room' })).toBeDisabled()

    rerender(<Harness readiness={guidance} />)
    expect(screen.getByText('all six gates pass')).toHaveClass('tone-ok')
    expect(screen.getByText('All gates pass. D-01 will capture pano_360 in room-01.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Capture room' })).toBeEnabled()
  })
})

describe('Control › Commands, Fleet and the mission tracker', () => {
  test('the catalogue lists every row, presses draft the same intent as the button, later rows are disabled', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')
    await openPane(user, 'Commands')

    const motion = within(screen.getByRole('group', { name: 'Motion commands' }))
    expect(motion.getAllByRole('button')).toHaveLength(11)
    expect(motion.getByRole('button', { name: /^Survey area/ })).toBeDisabled()
    expect(motion.getByRole('button', { name: /^Map area/ })).toBeDisabled()
    const rows = motion.getAllByRole('button')
    expect(rows[3]).toHaveTextContent('Land')
    expect(rows[3]).toHaveTextContent('unsupported')
    expect(rows[3]).toBeEnabled()
    expect(rows[0]).toHaveTextContent('Takeoff')
    expect(rows[0]).toHaveTextContent('accepted at M2.0')
    expect(screen.getByText('Relay reports none at 0.8 m.')).toBeInTheDocument()

    await user.click(motion.getByRole('button', { name: /^Come home/ }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({ name: 'come_home', selection: [1] })

    await user.click(within(screen.getByRole('group', { name: 'Altitude' })).getByRole('button', { name: 'Altitude up' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(2))
    expect(clients.console.sent[1]).toMatchObject({ name: 'altitude', args: { delta: 1 } })

    const steps = screen.getByLabelText('Steps per press')
    await user.clear(steps)
    await user.type(steps, '9')
    expect(steps).toHaveValue(6)
    await user.click(screen.getByRole('button', { name: 'Translate south' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(3))
    expect(clients.console.sent[2]).toMatchObject({ name: 'translate', args: { dx: 0, dy: -6 } })
  })

  test('the Fleet pane shows registry rows with metrics and reasons, the departed list, and a rejoin', async () => {
    const clients = {
      console: new FixtureRelayClient(session, () => t0, 'console', 'pending4'),
      keyboard: new FixtureRelayClient(session, () => t0, 'keyboard', 'pending4'),
    }
    const user = userEvent.setup()
    render(<App sessionId={session} clients={clients} intentDependencies={sequentialIds()} />)
    await screen.findByText('1 of 4 selected')
    await openPane(user, 'Fleet')

    expect(screen.getByText('Registry · roster v9')).toBeInTheDocument()
    const registry = within(screen.getByRole('region', { name: 'Registry' }))
    const d03 = within(registry.getByRole('article', { name: 'D-03 registry card' }))
    expect(d03.getByText('degraded')).toHaveClass('tone-warn')
    expect(d03.getByText('41%')).toHaveClass('tone-warn')
    expect(d03.getByText('12%')).toHaveClass('tone-danger')
    expect(d03.getByText('9 s ago')).toBeInTheDocument()
    expect(d03.getByText('telemetry_stale')).toBeInTheDocument()
    expect(d03.getByText(/Telemetry stopped inside the freshness window/)).toBeInTheDocument()
    expect(d03.getByRole('button', { name: 'Select D-03' })).toBeDisabled()
    const d04 = within(registry.getByRole('article', { name: 'D-04 registry card' }))
    expect(d04.getByText('RC takeover')).toHaveClass('tone-danger')
    expect(d04.getByText('RC safety operator absent')).toHaveClass('tone-danger')
    expect(d04.getByText('epoch 5')).toBeInTheDocument()

    const departed = screen.getByLabelText('D-05 departed')
    expect(departed).toHaveTextContent('adapter_connection_lost')
    expect(departed).toHaveTextContent('The adapter connection dropped without a leave.')
    expect(departed).toHaveTextContent('Has not rejoined.')

    clients.console.emitServer({
      v: 1,
      t: t0 + 5,
      type: 'membership',
      event_id: 'rejoin-5',
      session,
      roster_version: 10,
      action: 'join',
      drone_id: 5,
      connection_epoch: 4,
      membership: 'registered',
      readiness_reasons: ['telemetry_missing'],
      adapter_id: 'sim-05',
      capabilities: ['flight'],
      provenance: 'adapter_signature',
      reason: 'authenticated_rejoin',
    })
    expect(await screen.findByText('Rejoined as D-05 with a higher connection epoch (4).')).toBeInTheDocument()
    expect(registry.getByRole('article', { name: 'D-05 registry card' })).toHaveTextContent('registered')
    expect(screen.getByText('Registry · roster v10')).toBeInTheDocument()
  })

  test('the mission tracker advances a step per press, runs the clock from the first press, and resets', async () => {
    let now = t0
    const user = userEvent.setup()
    const { rerender } = render(<MissionTracker now={() => now} />)
    const rows = screen.getAllByRole('button', { name: /accepted at M2\.0|unsupported/ })
    expect(rows).toHaveLength(10)
    expect(rows[0]).toHaveAttribute('aria-current', 'step')
    expect(rows[4]).toHaveTextContent('formation_set')
    expect(rows[4]).toHaveTextContent('unsupported')
    expect(screen.getByLabelText('Elapsed')).toHaveTextContent('0:00')
    expect(screen.getByText(/Pass requires all ten steps/)).toBeInTheDocument()

    await user.click(rows[0])
    now += 65_000
    await user.click(rows[1])
    expect(rows[0]).toHaveTextContent('✓')
    expect(rows[2]).toHaveAttribute('aria-current', 'step')
    expect(screen.getByLabelText('Elapsed')).toHaveTextContent('1:05')

    for (let i = 2; i < 10; i += 1) await user.click(rows[i])
    expect(screen.getByText(/Pass — ten steps/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Reset the run' }))
    now += 10_000
    rerender(<MissionTracker now={() => now} />)
    expect(screen.getByLabelText('Elapsed')).toHaveTextContent('0:00')
    expect(rows[0]).toHaveAttribute('aria-current', 'step')
  })
})
