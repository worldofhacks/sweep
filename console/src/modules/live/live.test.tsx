import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { PlaybackDescriptor } from '../../media/playback'
import type { PlaybackSession, PlaybackStateListener } from '../../media/player'
import type { MediaRuntime } from '../../media/runtime'
import { UnavailableRelayClient } from '../../relay/client'
import { C1_BASIC_CONTROL_INTENTS, type DroneId, type RelayAircraftState } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft, fixtureScenario } from '../../testing/fixture-relay-client'

const session = 'live-module-session'
const clock = () => 1_756_700_000_000
const dependencies = { now: clock, nextId: () => 'live-module-intent' }

type User = ReturnType<typeof userEvent.setup>

function fixtureClients(fleet: 4 | 6 = 4) {
  return {
    console: new FixtureRelayClient(session, clock, 'console', fleet),
    keyboard: new FixtureRelayClient(session, clock, 'keyboard', fleet),
  }
}

function renderLive(clients: ReturnType<typeof fixtureClients>, media?: MediaRuntime) {
  return render(
    <App
      sessionId={session}
      clients={clients}
      intentDependencies={dependencies}
      initialModule="live"
      media={media}
    />,
  )
}

async function focusDevice(user: User, id = 'D-01') {
  await user.click(screen.getByRole('button', { name: `Focus ${id}` }))
}

async function returnToWall(user: User) {
  await user.click(screen.getByRole('button', { name: 'Back to All devices' }))
}

async function openModule(user: User, label: string) {
  await user.click(within(screen.getByRole('navigation', { name: 'Modules' })).getByRole('button', { name: label }))
}

function tile(id: string) {
  return within(screen.getByRole('article', { name: `${id} camera tile` }))
}

function emitState(
  client: FixtureRelayClient,
  eventId: string,
  drones: RelayAircraftState[],
  selection: DroneId[],
  rosterVersion = 7,
) {
  client.emitServer({
    v: 1,
    t: clock() + 1,
    type: 'state',
    event_id: eventId,
    session,
    roster_version: rosterVersion,
    armed: true,
    estop: false,
    selection,
    formation: 'none',
    spacing: 0.8,
    mode: 'indoor',
    capability_profile: 'c1_basic_control',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    pending: null,
    accepted_plan: null,
    drones,
  })
}

/** Records every playback session the module opens; nothing touches a network. */
class SessionLog {
  readonly started: string[] = []
  closed = 0
  readonly media: MediaRuntime = {
    configuration: {
      webrtcOrigin: 'http://ground-station:8889',
      readerUsername: 'reader',
      readerPassword: 'secret',
    },
    createSession: () => new LoggedSession(this),
  }
}

class LoggedSession implements PlaybackSession {
  private readonly log: SessionLog

  constructor(log: SessionLog) {
    this.log = log
  }

  async start(
    _video: HTMLVideoElement,
    descriptor: PlaybackDescriptor,
    onState: PlaybackStateListener,
  ): Promise<void> {
    this.log.started.push(descriptor.stream)
    onState('connecting')
    onState('playing')
  }

  async close(): Promise<void> {
    this.log.closed += 1
  }
}

describe('Live module wall', () => {
  test('All devices is the only wall and shows each reported aircraft with its stream state in words', async () => {
    const clients = fixtureClients()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    expect(screen.getByRole('heading', { name: 'All devices', level: 1 })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Live panes' })).not.toBeInTheDocument()
    for (const name of ['Wall of 4', 'Wall of 6', 'Ground', 'Focus feed']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    const wall = within(screen.getByRole('region', { name: 'All devices' }))
    expect(wall.getAllByRole('article')).toHaveLength(4)
    expect(wall.queryByRole('article', { name: /Slot \d empty/ })).not.toBeInTheDocument()
    expect(
      wall.getByText(
        '4 reported devices. New devices appear automatically; offline feeds keep their place.',
      ),
    ).toBeInTheDocument()

    const one = tile('D-01')
    expect(one.getByText('live')).toBeInTheDocument()
    expect(one.getByText('just now')).toBeInTheDocument()
    expect(one.getByText('bat 78%')).toBeInTheDocument()
    expect(one.getByText('link 96%')).toBeInTheDocument()
    expect(one.getByText('pos 92%')).toBeInTheDocument()
    expect(one.getAllByText('ready')).toHaveLength(2)
    expect(one.queryByText(/No video/)).not.toBeInTheDocument()

    const two = tile('D-02')
    expect(two.getByText('offline')).toBeInTheDocument()
    expect(two.getByText('12 s ago')).toBeInTheDocument()
    expect(two.getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()

    const three = tile('D-03')
    expect(three.getByText('degraded')).toBeInTheDocument()
    expect(three.getByText('Telemetry stale, Camera not ready')).toBeInTheDocument()

    const four = tile('D-04')
    expect(four.getByText('unreported')).toBeInTheDocument()
    expect(four.getByText('no frame reported')).toBeInTheDocument()
    expect(
      four.getByText('No video reported. The console shows unreported rather than inventing a state.'),
    ).toBeInTheDocument()
  })

  test('the wall follows the reported roster size without truncation or empty slots', async () => {
    const six = renderLive(fixtureClients(6))
    await screen.findByText(/Development fixture active/i)
    const wall = within(screen.getByRole('region', { name: 'All devices' }))
    expect(wall.getAllByRole('article')).toHaveLength(6)
    expect(wall.getAllByRole('button', { name: /^Focus D-/ })).toHaveLength(6)
    expect(wall.queryByRole('article', { name: /Slot \d empty/ })).not.toBeInTheDocument()
    expect(tile('D-05').getByText('live')).toBeInTheDocument()
    expect(tile('D-06').getByText('unreported')).toBeInTheDocument()
    six.unmount()

    renderLive(fixtureClients(4))
    await screen.findByText(/Development fixture active/i)
    const smaller = within(screen.getByRole('region', { name: 'All devices' }))
    expect(smaller.getAllByRole('article')).toHaveLength(4)
    expect(smaller.queryByRole('article', { name: /Slot \d empty/ })).not.toBeInTheDocument()
  })

  test('tile selection toggles send a real select intent and respect the relay gates', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    const one = tile('D-01').getByRole('button', { name: 'in selection D-01' })
    expect(one).toHaveAttribute('aria-pressed', 'true')
    expect(one).toBeDisabled()
    expect(one).toHaveAttribute('title', 'Intent v1 requires at least one aircraft in a select request.')
    const three = tile('D-03').getByRole('button', { name: 'not selectable D-03' })
    expect(three).toBeDisabled()
    expect(three).toHaveAttribute('title', 'Relay reports this aircraft is not selectable.')

    await user.click(tile('D-02').getByRole('button', { name: 'add to selection D-02' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      name: 'select',
      source: 'console',
      args: { ids: [1, 2] },
      selection: [1, 2],
    })
    expect(await tile('D-02').findByRole('button', { name: 'in selection D-02' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(tile('D-01').getByRole('button', { name: 'in selection D-01' })).toBeEnabled()
  })

  test('an empty roster shows where future cameras will appear without invented tiles', async () => {
    render(
      <App
        sessionId={session}
        clients={{
          console: new UnavailableRelayClient('Console relay missing.', clock),
          keyboard: new UnavailableRelayClient('Keyboard relay missing.', clock),
        }}
        intentDependencies={dependencies}
        initialModule="live"
      />,
    )
    const empty = (await screen.findByText('Nothing to show')).closest('[role="status"]')
    expect(empty).toHaveTextContent('No devices have joined this session. Their cameras appear here as they join.')
    expect(screen.queryByRole('region', { name: 'All devices' })).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Live panes' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Back to All devices' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /Focused (aircraft|device)/ })).not.toBeInTheDocument()
  })
})

describe('Live module focus', () => {
  test('focus follows a single selection, survives video loss, and clears only when the aircraft leaves', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)
    expect(screen.getByRole('button', { name: 'Focus D-01' })).toHaveAttribute('aria-pressed', 'true')

    const drones = fixtureAircraft(clock())
    drones[0] = { ...drones[0], video: { status: 'offline', last_frame_at: clock() - 3_000 } }
    emitState(clients.console, 'state-focused-video-lost', drones, [1])
    await focusDevice(user)
    const focused = within(await screen.findByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.getByText('drone1')).toBeInTheDocument()
    expect(focused.getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()
    expect(focused.getAllByText('3 s ago')).toHaveLength(2)
    expect(focused.getByText('offline', { selector: 'dd' })).toHaveClass('tone-warn')
    expect(focused.getByText('none requested')).toBeInTheDocument()
    expect(focused.getByText('guidance mode').nextElementSibling).toHaveTextContent('unreported')

    emitState(clients.console, 'state-selection-moves', drones, [2])
    expect(await screen.findByRole('region', { name: 'Focused aircraft D-02' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Focused aircraft D-01' })).not.toBeInTheDocument()

    emitState(clients.console, 'state-focused-departed', drones.filter((drone) => drone.drone_id !== 2), [])
    const none = within(await screen.findByRole('region', { name: 'Focused device none' }))
    expect(none.getByText(/Nothing is focused/)).toBeInTheDocument()
    expect(none.getByText(/No device is focused/)).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('an explicit focus from a wall is local to the console and survives module switching', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: 'Focus D-04' }))
    expect(screen.getByRole('region', { name: 'Focused aircraft D-04' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'All devices' })).not.toBeInTheDocument()
    await returnToWall(user)
    expect(screen.getByRole('button', { name: 'Focus D-04' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Focus D-01' })).toHaveAttribute('aria-pressed', 'false')

    await openModule(user, 'Reference')
    await openModule(user, 'Live')
    expect(screen.getByRole('button', { name: 'Focus D-04' })).toHaveAttribute('aria-pressed', 'true')
    await focusDevice(user, 'D-04')
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-04' }))
    expect(focused.getByText('stream status').nextElementSibling).toHaveTextContent('unreported')
    expect(focused.getByText('last frame').nextElementSibling).toHaveTextContent('no frame reported')
    expect(clients.console.sent).toHaveLength(0)
  })

  test('capture progress follows the newest capture_room request that targets the focused aircraft', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    await openModule(user, 'Control')
    await user.click(within(screen.getByRole('group', { name: 'Control panes' })).getByRole('button', { name: 'Capture' }))
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    await user.click(screen.getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))

    await openModule(user, 'Live')
    await focusDevice(user)
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.getByText('capture progress').nextElementSibling).toHaveTextContent('accepted')
  })
})

describe('Live module playback', () => {
  test('the default wall grows from aircraft to a joining robot and all eight configured devices without empty slots or restarting existing feeds', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    const view = renderLive(clients, log.media)
    await screen.findByRole('region', { name: 'All devices' })
    expect(screen.queryByRole('group', { name: 'Live panes' })).not.toBeInTheDocument()
    const aircraft = fixtureAircraft(clock()).map((device) => ({
      ...device, video: { status: 'live' as const, last_frame_at: clock() },
    }))
    act(() => emitState(clients.console, 'two-aircraft-online', aircraft.slice(0, 2), [1]))
    const wall = within(screen.getByRole('region', { name: 'All devices' }))
    expect(wall.getAllByRole('article')).toHaveLength(2)
    expect(wall.queryByRole('article', { name: /Slot .* empty/ })).not.toBeInTheDocument()
    await waitFor(() => expect(log.started).toEqual(['drone1', 'drone2']))
    const firstPlayer = tile('D-01').getByLabelText('Live feed D-01')
    const robot = fixtureScenario('mixed').fleet(clock()).find((device) => device.drone_id === 11)!
    act(() => emitState(clients.console, 'robot-joining', [...aircraft.slice(0, 2), {
      ...robot, membership: 'registered', selectable: false,
      readiness_reasons: ['telemetry_missing'], video: undefined,
    }], [1], 8))
    expect(wall.getAllByRole('article')).toHaveLength(3)
    expect(tile('G-01').getByText('registered')).toBeInTheDocument()
    expect(tile('G-01').getByText('unreported')).toBeInTheDocument()
    expect(tile('G-01').queryByLabelText('Live feed G-01')).not.toBeInTheDocument()
    expect(log.started).toEqual(['drone1', 'drone2'])
    const robots = Array.from({ length: 4 }, (_, index) => ({
      ...robot, drone_id: 11 + index, unit: index + 1,
      video: { status: 'live' as const, last_frame_at: clock() },
    }))
    act(() => emitState(clients.console, 'eight-devices-online', [...aircraft, ...robots], [1], 9))
    expect(wall.getAllByRole('article')).toHaveLength(8)
    expect(wall.queryByRole('article', { name: /Slot .* empty/ })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(8))
    expect(log.started).toEqual(['drone1', 'drone2', 'drone3', 'drone4', 'ground1', 'ground2', 'ground3', 'ground4'])
    expect(tile('D-01').getByLabelText('Live feed D-01')).toBe(firstPlayer)
    expect(log.closed).toBe(0)
    expect(clients.console.sent).toEqual([])
    view.unmount()
    expect(log.closed).toBe(8)
  })

  test('selecting a robot camera uses its global device id alongside an aircraft selection', async () => {
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
    }
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByRole('region', { name: 'All devices' })
    await user.click(tile('G-02').getByRole('button', { name: 'add to selection G-02' }))
    expect(clients.console.sent).toHaveLength(1)
    expect(clients.console.sent[0]).toMatchObject({
      name: 'select', source: 'console', selection: [1, 12], args: { ids: [1, 12] },
    })
    expect(clients.keyboard.sent).toEqual([])
  })

  test('the mixed wall plays all five class/unit paths and reconnects only the device whose epoch changed', async () => {
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
    }
    const log = new SessionLog()
    const view = renderLive(clients, log.media)
    await screen.findByRole('region', { name: 'All devices' })
    await waitFor(() => expect(log.started).toEqual(['drone1', 'ground1', 'ground2']))
    const drones = fixtureScenario('mixed').fleet(clock()).map((drone) => ({
      ...drone, video: { status: 'live' as const, last_frame_at: clock() },
    }))
    const rosterVersion = fixtureScenario('mixed').rosterVersion
    act(() => emitState(clients.console, 'five-live', drones, [1], rosterVersion))
    await waitFor(() => expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(5))
    expect(log.started).toEqual(['drone1', 'ground1', 'ground2', 'drone2', 'ground3'])
    expect(log.closed).toBe(0)
    for (const id of ['D-01', 'D-02', 'G-01', 'G-02', 'G-03']) {
      expect(tile(id).getByLabelText(`Live feed ${id}`)).toBeInTheDocument()
    }
    const rejoined = drones.map((drone) => drone.drone_id === 12
      ? { ...drone, connection_epoch: drone.connection_epoch + 1 }
      : drone)
    act(() => emitState(clients.console, 'ground-two-rejoined', rejoined, [1], rosterVersion))
    await waitFor(() => expect(log.closed).toBe(1))
    expect(log.started).toEqual(['drone1', 'ground1', 'ground2', 'drone2', 'ground3', 'ground2'])
    expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(5)
    act(() => emitState(clients.console, 'ground-one-offline', rejoined.map((drone) => drone.drone_id === 11
      ? { ...drone, video: { status: 'offline', last_frame_at: clock() } }
      : drone), [1], rosterVersion))
    await waitFor(() => expect(log.closed).toBe(2))
    expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(4)
    expect(tile('G-01').getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()
    expect(clients.console.sent).toEqual([])
    expect(clients.keyboard.sent).toEqual([])
    view.unmount()
    expect(log.closed).toBe(6)
  })

  test('a wall tile plays only while the relay reports its stream live, and the focus feed follows the focused aircraft', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    const user = userEvent.setup()
    renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)

    // Fixture: D-01 live, D-02 and D-03 offline, D-04 unreported: exactly one tile plays.
    expect(tile('D-01').getByLabelText('Live feed D-01')).toBeInTheDocument()
    expect(await tile('D-01').findByText('Playback playing')).toBeInTheDocument()
    expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(1)
    expect(log.started).toEqual(['drone1'])
    expect(tile('D-01').queryByText(/Playback is not configured/)).not.toBeInTheDocument()
    expect(tile('D-02').getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()

    await focusDevice(user)
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.getByLabelText('Live feed D-01')).toBeInTheDocument()
    expect(await focused.findByText('Playback playing')).toBeInTheDocument()
    // Leaving the wall closed its player; the focus feed opened its own session.
    await waitFor(() => expect(log.closed).toBe(1))
    expect(log.started).toEqual(['drone1', 'drone1'])
    expect(focused.queryByText(/Playback is not configured/)).not.toBeInTheDocument()

    await returnToWall(user)
    await waitFor(() => expect(log.closed).toBe(2))
    expect(log.started).toEqual(['drone1', 'drone1', 'drone1'])

    await focusDevice(user, 'D-02')
    const offline = within(screen.getByRole('region', { name: 'Focused aircraft D-02' }))
    expect(offline.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(offline.getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()
    await waitFor(() => expect(log.closed).toBe(3))
    expect(log.started).toEqual(['drone1', 'drone1', 'drone1'])
  })

  test('four live tiles hold four concurrent sessions that close when device inspection opens', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    const user = userEvent.setup()
    renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)
    await waitFor(() => expect(log.started).toEqual(['drone1']))

    const drones = fixtureAircraft(clock()).map((drone) => ({
      ...drone,
      video: { status: 'live' as const, last_frame_at: clock() - 100 },
    }))
    emitState(clients.console, 'state-all-live', drones, [1])

    await waitFor(() => expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(4))
    for (const id of ['D-01', 'D-02', 'D-03', 'D-04']) {
      expect(await tile(id).findByText('Playback playing')).toBeInTheDocument()
    }
    // D-01's session survived the state update; the other three opened once each.
    expect(log.started).toEqual(['drone1', 'drone2', 'drone3', 'drone4'])
    expect(log.closed).toBe(0)

    await focusDevice(user)
    await waitFor(() => expect(log.closed).toBe(4))
    expect(screen.getAllByLabelText(/Live feed/)).toHaveLength(1)
  })

  test('a tile tears its player down the moment the relay reports the stream dropped and says the age', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)
    await waitFor(() => expect(log.started).toEqual(['drone1']))

    const drones = fixtureAircraft(clock())
    drones[0] = { ...drones[0], video: { status: 'offline', last_frame_at: clock() - 7_000 } }
    emitState(clients.console, 'state-d1-dropped', drones, [1])

    await waitFor(() => expect(log.closed).toBe(1))
    const one = tile('D-01')
    expect(one.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(one.getByText('offline')).toBeInTheDocument()
    expect(one.getByText('7 s ago')).toBeInTheDocument()
    expect(one.getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()
    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()

    drones[0] = { ...drones[0], video: { status: 'live', last_frame_at: clock() - 50 } }
    emitState(clients.console, 'state-d1-back', drones, [1])
    await waitFor(() => expect(log.started).toEqual(['drone1', 'drone1']))
    expect(await tile('D-01').findByText('Playback playing')).toBeInTheDocument()
    expect(tile('D-01').getByText('just now')).toBeInTheDocument()
  })

  test('unmounting the console closes every open session', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    const view = renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)
    await waitFor(() => expect(log.started).toEqual(['drone1']))

    view.unmount()
    await waitFor(() => expect(log.closed).toBe(1))
  })

  test('a live stream without a media bootstrap says playback is not configured', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(tile('D-01').getByText('Playback is not configured on this console.')).toBeInTheDocument()
    expect(tile('D-02').queryByText(/Playback is not configured/)).not.toBeInTheDocument()

    await focusDevice(user)
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(focused.getByText(/Playback is not configured on this console/)).toBeInTheDocument()
    expect(focused.getByText('live', { selector: 'dd' })).toHaveClass('tone-ok')
  })

  test('a failed negotiation is reported beside the feed while the relay still says live', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    const media: MediaRuntime = {
      configuration: {
        webrtcOrigin: 'http://ground-station:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      },
      createSession: () => ({
        async start(_video, _descriptor, onState) {
          onState('connecting')
          onState('failed', 'WHEP negotiation failed with 503')
        },
        async close() {},
      }),
    }
    renderLive(clients, media)
    await screen.findByText(/Development fixture active/i)

    await focusDevice(user)
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(
      await focused.findByText(
        'Playback failed: WHEP negotiation failed with 503. The relay still reports the stream live.',
      ),
    ).toHaveClass('is-failed')
    expect(focused.getByText('live', { selector: 'dd' })).toBeInTheDocument()
  })
})

describe('Live module robot inspection', () => {
  test('robots share All devices with aircraft and open inspection directly without sending an intent', async () => {
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
    }
    const log = new SessionLog()
    const user = userEvent.setup()
    renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)
    const wall = within(screen.getByRole('region', { name: 'All devices' }))
    expect(wall.getAllByRole('article').map((article) => article.getAttribute('aria-label'))).toEqual([
      'D-01 camera tile', 'D-02 camera tile', 'G-01 camera tile', 'G-02 camera tile', 'G-03 camera tile',
    ])
    expect(tile('G-01').getByLabelText('Live feed G-01')).toBeInTheDocument()
    expect(tile('G-02').getByLabelText('Live feed G-02')).toBeInTheDocument()
    expect(await tile('G-01').findByText('Playback playing')).toBeInTheDocument()
    await waitFor(() => expect(log.started).toEqual(['drone1', 'ground1', 'ground2']))
    expect(tile('G-03').getByText('unreported')).toBeInTheDocument()
    expect(tile('G-03').getByRole('button', { name: 'not selectable G-03' })).toHaveAttribute(
      'title', 'Relay reports this robot is not selectable.',
    )

    await focusDevice(user, 'G-02')
    const focused = within(screen.getByRole('region', { name: 'Focused robot G-02' }))
    expect(focused.getByText('ground2')).toBeInTheDocument()
    expect(focused.getByLabelText('Live feed G-02')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'All devices' })).not.toBeInTheDocument()
    await returnToWall(user)
    expect(screen.getByRole('region', { name: 'All devices' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Focused robot G-02' })).not.toBeInTheDocument()
    expect(tile('G-02').getByRole('button', { name: 'Focus G-02' })).toHaveAttribute('aria-pressed', 'true')
    expect(clients.console.sent).toEqual([])
    expect(clients.keyboard.sent).toEqual([])
  })
})
