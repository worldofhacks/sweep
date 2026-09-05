import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import App from '../../App'
import type { PlaybackDescriptor } from '../../media/playback'
import type { PlaybackSession, PlaybackStateListener } from '../../media/player'
import type { MediaRuntime } from '../../media/runtime'
import { UnavailableRelayClient } from '../../relay/client'
import type { DroneId, RelayAircraftState } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'

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

function livePanes() {
  return within(screen.getByRole('group', { name: 'Live panes' }))
}

async function openPane(user: User, label: 'Wall of 4' | 'Wall of 6' | 'Focus feed') {
  await user.click(livePanes().getByRole('button', { name: label }))
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
) {
  client.emitServer({
    v: 1,
    t: clock() + 1,
    type: 'state',
    event_id: eventId,
    session,
    roster_version: 7,
    armed: true,
    estop: false,
    selection,
    formation: 'none',
    spacing: 0.8,
    mode: 'indoor',
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

describe('Live module walls', () => {
  test('the wall of four shows one tile per reported aircraft with its stream state in words', async () => {
    const clients = fixtureClients()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    expect(livePanes().getByRole('button', { name: 'Wall of 4' })).toHaveAttribute('aria-pressed', 'true')
    const wall = within(screen.getByRole('region', { name: 'Wall of 4' }))
    expect(wall.getAllByRole('article')).toHaveLength(4)
    expect(wall.queryByRole('article', { name: /Slot \d empty/ })).not.toBeInTheDocument()
    expect(
      wall.getByText(
        "4 tiles. Focus follows the operator's selection and survives video loss on the focused aircraft.",
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
    expect(three.getByText('telemetry_stale, camera_not_ready')).toBeInTheDocument()

    const four = tile('D-04')
    expect(four.getByText('unreported')).toBeInTheDocument()
    expect(four.getByText('no frame reported')).toBeInTheDocument()
    expect(
      four.getByText('No video reported. The console shows unreported rather than inventing a state.'),
    ).toBeInTheDocument()
  })

  test('the wall of six shows six tiles, and empty slots stay empty for a smaller fleet', async () => {
    const user = userEvent.setup()
    const six = renderLive(fixtureClients(6))
    await screen.findByText(/Development fixture active/i)
    expect(
      screen.getByText(/The relay reports 6 aircraft; the first 4 by id are shown\./),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Focus D-/ })).toHaveLength(4)

    await openPane(user, 'Wall of 6')
    const wall = within(screen.getByRole('region', { name: 'Wall of 6' }))
    expect(wall.getAllByRole('article')).toHaveLength(6)
    expect(wall.getAllByRole('button', { name: /^Focus D-/ })).toHaveLength(6)
    expect(wall.queryByRole('article', { name: /Slot \d empty/ })).not.toBeInTheDocument()
    expect(tile('D-05').getByText('live')).toBeInTheDocument()
    expect(tile('D-06').getByText('unreported')).toBeInTheDocument()
    six.unmount()

    renderLive(fixtureClients(4))
    await screen.findByText(/Development fixture active/i)
    await openPane(user, 'Wall of 6')
    const smaller = within(screen.getByRole('region', { name: 'Wall of 6' }))
    expect(smaller.getAllByRole('article')).toHaveLength(6)
    expect(smaller.getAllByRole('button', { name: /^Focus D-/ })).toHaveLength(4)
    expect(smaller.getByRole('article', { name: 'Slot 5 empty' })).toHaveTextContent('no aircraft')
    expect(smaller.getByRole('article', { name: 'Slot 6 empty' })).toHaveTextContent(
      'No aircraft reported for this slot.',
    )
    expect(smaller.getByText(/4 of 6 slots have a reported aircraft\./)).toBeInTheDocument()
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

  test('an empty roster renders an honest empty state in every pane', async () => {
    const user = userEvent.setup()
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
    expect(empty).toHaveTextContent('No aircraft have joined this session, so there is no wall and nothing to focus.')
    expect(screen.queryByRole('region', { name: 'Wall of 4' })).not.toBeInTheDocument()
    await openPane(user, 'Focus feed')
    expect(screen.getByText(/no wall and nothing to focus/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /Focused aircraft/ })).not.toBeInTheDocument()
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
    await openPane(user, 'Focus feed')
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
    const none = within(await screen.findByRole('region', { name: 'Focused aircraft none' }))
    expect(none.getByText(/Nothing is focused/)).toBeInTheDocument()
    expect(none.getByText(/No aircraft is focused/)).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('an explicit focus from a wall is local to the console and survives module switching', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    await user.click(screen.getByRole('button', { name: 'Focus D-04' }))
    expect(screen.getByRole('button', { name: 'Focus D-04' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Focus D-01' })).toHaveAttribute('aria-pressed', 'false')

    await openModule(user, 'Reference')
    await openModule(user, 'Live')
    expect(screen.getByRole('button', { name: 'Focus D-04' })).toHaveAttribute('aria-pressed', 'true')
    await openPane(user, 'Focus feed')
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
    await openPane(user, 'Focus feed')
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.getByText('capture progress').nextElementSibling).toHaveTextContent('accepted')
  })
})

describe('Live module playback', () => {
  test('the player mounts only while the relay reports the focused stream live and playback is configured', async () => {
    const clients = fixtureClients()
    const log = new SessionLog()
    const user = userEvent.setup()
    renderLive(clients, log.media)
    await screen.findByText(/Development fixture active/i)

    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(log.started).toEqual([])

    await openPane(user, 'Focus feed')
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(focused.getByLabelText('Live feed D-01')).toBeInTheDocument()
    expect(await focused.findByText('Playback playing')).toBeInTheDocument()
    expect(log.started).toEqual(['drone1'])
    expect(focused.queryByText(/Playback is not configured/)).not.toBeInTheDocument()

    await openPane(user, 'Wall of 4')
    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    await waitFor(() => expect(log.closed).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Focus D-02' }))
    await openPane(user, 'Focus feed')
    const offline = within(screen.getByRole('region', { name: 'Focused aircraft D-02' }))
    expect(offline.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(offline.getByText('No video. The adapter reports the stream offline.')).toBeInTheDocument()
    expect(log.started).toEqual(['drone1'])
  })

  test('a live stream without a media bootstrap says playback is not configured', async () => {
    const clients = fixtureClients()
    const user = userEvent.setup()
    renderLive(clients)
    await screen.findByText(/Development fixture active/i)

    await openPane(user, 'Focus feed')
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

    await openPane(user, 'Focus feed')
    const focused = within(screen.getByRole('region', { name: 'Focused aircraft D-01' }))
    expect(
      await focused.findByText(
        'Playback failed: WHEP negotiation failed with 503. The relay still reports the stream live.',
      ),
    ).toHaveClass('is-failed')
    expect(focused.getByText('live', { selector: 'dd' })).toBeInTheDocument()
  })
})
