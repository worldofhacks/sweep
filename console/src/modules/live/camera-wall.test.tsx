import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from '../../App'
import { cameraTarget, cameraTargetKey } from '../../media/cameras'
import type { PlaybackDescriptor } from '../../media/playback'
import type { PlaybackStateListener } from '../../media/player'
import type { MediaRuntime } from '../../media/runtime'
import { C1_BASIC_CONTROL_INTENTS, type RelayAircraftState } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft, fixtureScenario } from '../../testing/fixture-relay-client'
import { CameraMosaic } from './CameraMosaic'
import { FocusFeed } from './FocusFeed'

const now = 1_756_700_000_000
const session = 'six-camera-tests'

function fleet(): RelayAircraftState[] {
  const aircraft = fixtureAircraft(now).slice(0, 2).map((device) => ({
    ...device,
    cameras: [{ camera_id: 'fpv', label: 'Flight camera', stream: `drone${device.unit}`, status: 'live' as const, last_frame_at: now }],
  }))
  const ground = fixtureScenario('mixed').fleet(now).filter((device) => device.device_class === 'ground_vehicle').slice(0, 2).map((device) => ({
    ...device, node_type: 'ground' as const,
    cameras: ['head', 'rear'].map((camera) => ({
      camera_id: camera, label: camera === 'head' ? 'Head camera' : 'Rear camera',
      stream: `ground${device.unit}-${camera}`, status: 'live' as const, last_frame_at: now,
    })),
  }))
  return [...aircraft, ...ground]
}

function playbackLog() {
  const sessions: { stream: string; listener: PlaybackStateListener; close: ReturnType<typeof vi.fn> }[] = []
  const media: MediaRuntime = {
    configuration: { webrtcOrigin: 'http://media.test:8889', readerUsername: 'test-reader', readerPassword: 'test-password' },
    createSession: () => {
      const close = vi.fn(async () => undefined)
      return {
        start: async (_video: HTMLVideoElement, descriptor: PlaybackDescriptor, listener: PlaybackStateListener) => {
          sessions.push({ stream: descriptor.stream, listener, close })
          listener('connecting')
        },
        close,
      }
    },
  }
  return { sessions, media }
}

const tile = (name: string, cameraId: string) => within(screen.getByRole('article', { name: `${name} (${cameraId}) camera tile` }))

describe('explicit camera wall', () => {
  test('two ground pairs plus two aircraft produce six distinct streams and visible independent playback evidence', async () => {
    const log = playbackLog()
    const onFocus = vi.fn()
    const devices = fleet()
    const { unmount } = render(<CameraMosaic devices={devices} now={now} focused={null} onFocus={onFocus} media={log.media} />)
    expect(screen.getAllByRole('article')).toHaveLength(6)
    expect(screen.getByText(/6 configured cameras across 4 devices/)).toBeInTheDocument()
    await waitFor(() => expect(log.sessions).toHaveLength(6))
    expect(log.sessions.map((entry) => entry.stream)).toEqual([
      'drone1', 'drone2', 'ground1-head', 'ground1-rear', 'ground2-head', 'ground2-rear',
    ])
    const rear = tile('G-01 · Rear camera', 'rear')
    expect(rear.getByText('Source live')).toBeInTheDocument()
    expect(rear.queryByText('Playback · receiving frames')).not.toBeInTheDocument()
    act(() => log.sessions[3].listener('playing'))
    expect(rear.getByText('Playback · receiving frames')).toBeVisible()
    expect(tile('G-01 · Head camera', 'head').queryByText('Playback · receiving frames')).not.toBeInTheDocument()
    await userEvent.setup().click(rear.getByRole('button', { name: 'Focus G-01 · Rear camera (rear)' }))
    expect(onFocus).toHaveBeenCalledWith(cameraTarget(devices[2], devices[2].cameras![1]))
    expect(screen.queryByRole('button', { name: /selection/ })).not.toBeInTheDocument()
    unmount()
    expect(log.sessions.every((entry) => entry.close.mock.calls.length === 1)).toBe(true)
  })

  test('missing and empty inventories never synthesize camera tiles from legacy video', () => {
    const devices = fleet().map((device, index) => ({ ...device, cameras: index % 2 ? [] : undefined }))
    const log = playbackLog()
    render(<CameraMosaic devices={devices} now={now} focused={null} onFocus={vi.fn()} media={log.media} />)
    expect(screen.queryAllByRole('article')).toHaveLength(0)
    expect(screen.getByText(/no camera slots are invented/)).toBeInTheDocument()
    expect(log.sessions).toHaveLength(0)
  })

  test('duplicate stream mappings are withheld instead of showing one feed as two cameras', async () => {
    const devices = fleet()
    devices[2].cameras![1] = { ...devices[2].cameras![1], stream: devices[3].cameras![1].stream }
    const log = playbackLog()
    render(<CameraMosaic devices={devices} now={now} focused={null} onFocus={vi.fn()} media={log.media} />)
    expect(screen.getByText(/2 camera entries share a stream name/)).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(4)
    await waitFor(() => expect(log.sessions).toHaveLength(4))
    expect(log.sessions.some((entry) => entry.stream === 'ground2-rear')).toBe(false)
  })

  test.each(['stale', 'future', 'offline', 'removed'] as const)('%s camera evidence retires only its player', async (change) => {
    const log = playbackLog()
    let devices = fleet()
    const view = () => <CameraMosaic devices={devices} now={now} focused={null} onFocus={vi.fn()} media={log.media} />
    const { rerender } = render(view())
    await waitFor(() => expect(log.sessions).toHaveLength(6))
    devices = devices.map((device) => device.drone_id !== 11 ? device : {
      ...device,
      cameras: change === 'removed' ? device.cameras!.slice(0, 1) : device.cameras!.map((camera) => camera.camera_id !== 'rear' ? camera : {
        ...camera, status: change === 'offline' ? 'offline' : 'live',
        last_frame_at: change === 'stale' ? now - 6000 : change === 'future' ? now + 100 : now,
      }),
    })
    rerender(view())
    await waitFor(() => expect(log.sessions[3].close).toHaveBeenCalledTimes(1))
    expect(log.sessions.filter((_, index) => index !== 3).every((entry) => entry.close.mock.calls.length === 0)).toBe(true)
    expect(screen.queryByLabelText('Live feed G-01 · Rear camera')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Live feed G-01 · Head camera')).toBeInTheDocument()
  })

  test('device loss retires both cameras, while epoch and stream changes recreate only the affected identities', async () => {
    const log = playbackLog()
    let devices = fleet()
    const view = () => <CameraMosaic devices={devices} now={now} focused={null} onFocus={vi.fn()} media={log.media} />
    const { rerender } = render(view())
    await waitFor(() => expect(log.sessions).toHaveLength(6))
    devices = devices.map((device) => device.drone_id === 11 ? { ...device, connection_epoch: device.connection_epoch + 1 } : device)
    rerender(view())
    await waitFor(() => expect(log.sessions).toHaveLength(8))
    expect(log.sessions[2].close).toHaveBeenCalledTimes(1)
    expect(log.sessions[3].close).toHaveBeenCalledTimes(1)
    expect(log.sessions[4].close).not.toHaveBeenCalled()
    devices = devices.map((device) => device.drone_id === 12 ? {
      ...device, cameras: device.cameras!.map((camera) => camera.camera_id === 'rear' ? { ...camera, stream: 'ground2-rear-new' } : camera),
    } : device)
    rerender(view())
    await waitFor(() => expect(log.sessions.at(-1)!.stream).toBe('ground2-rear-new'))
    expect(log.sessions[5].close).toHaveBeenCalledTimes(1)
    expect(log.sessions[4].close).not.toHaveBeenCalled()
    devices = devices.map((device) => device.drone_id === 12 ? { ...device, membership: 'disconnected' } : device)
    rerender(view())
    await waitFor(() => expect(log.sessions[4].close).toHaveBeenCalledTimes(1))
    expect(log.sessions.at(-1)!.close).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Live feed G-01 · Head camera')).toBeInTheDocument()
  })

  test('ordinary roster updates preserve all six sessions', async () => {
    const log = playbackLog()
    const devices = fleet()
    const view = (items: RelayAircraftState[]) => <CameraMosaic devices={items} now={now} focused={null} onFocus={vi.fn()} media={log.media} />
    const { rerender } = render(view(devices))
    await waitFor(() => expect(log.sessions).toHaveLength(6))
    rerender(view(devices.map((device) => ({ ...device, battery: 0.72, cameras: device.cameras!.map((camera) => ({ ...camera })) }))))
    expect(log.sessions).toHaveLength(6)
    expect(log.sessions.every((entry) => entry.close.mock.calls.length === 0)).toBe(true)
  })

  test('identical labels retain distinct camera IDs and relay loss closes every player', async () => {
    const log = playbackLog()
    let devices = fleet().map((device) => ({ ...device, cameras: device.cameras!.map((camera) => ({ ...camera, label: 'Camera' })) }))
    const view = () => <CameraMosaic devices={devices} now={now} focused={null} onFocus={vi.fn()} media={log.media} />
    const { rerender } = render(view())
    expect(screen.getByRole('button', { name: 'Focus G-01 · Camera (head)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Focus G-01 · Camera (rear)' })).toBeInTheDocument()
    await waitFor(() => expect(log.sessions).toHaveLength(6))
    devices = devices.map((device) => ({ ...device, membership: 'disconnected' }))
    rerender(view())
    await waitFor(() => expect(log.sessions.every((entry) => entry.close.mock.calls.length === 1)).toBe(true))
    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
  })
})

describe('camera-wall inspection', () => {
  test('a removed camera does not revive an old inspection when its identity reappears', async () => {
    const log = playbackLog()
    const original = fleet()[2]
    let device = original
    let target = cameraTarget(device, device.cameras![1])
    const view = () => <FocusFeed focused={device} target={target} requests={[]} now={now} media={log.media} />
    const { rerender } = render(view())
    await waitFor(() => expect(log.sessions).toHaveLength(1))
    device = { ...original, cameras: original.cameras!.slice(0, 1) }
    rerender(view())
    device = original
    rerender(view())
    expect(log.sessions).toHaveLength(1)
    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    target = cameraTarget(device, device.cameras![1])
    rerender(view())
    await waitFor(() => expect(log.sessions).toHaveLength(2))
    expect(log.sessions[1].stream).toBe('ground1-rear')
  })

  test.each(['epoch', 'stream', 'removed'] as const)('%s changes cannot silently redirect an inspected camera', async (change) => {
    const log = playbackLog()
    let device = fleet()[2]
    const target = cameraTarget(device, device.cameras![1])
    const onTargetChange = vi.fn()
    const view = () => <FocusFeed focused={device} target={target} onTargetChange={onTargetChange} requests={[]} now={now} media={log.media} />
    const { rerender } = render(view())
    await waitFor(() => expect(log.sessions.map((entry) => entry.stream)).toEqual(['ground1-rear']))
    device = change === 'epoch' ? { ...device, connection_epoch: device.connection_epoch + 1 }
      : { ...device, cameras: change === 'removed' ? device.cameras!.slice(0, 1)
        : device.cameras!.map((camera) => camera.camera_id === 'rear' ? { ...camera, stream: 'replacement' } : camera) }
    rerender(view())
    await waitFor(() => expect(log.sessions[0].close).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/inspected camera connection is no longer available/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Live feed/)).not.toBeInTheDocument()
    expect(log.sessions).toHaveLength(1)
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'G-01 camera' }), 'head')
    expect(onTargetChange).toHaveBeenCalledWith(cameraTarget(device, device.cameras![0]))
    expect(cameraTargetKey(target)).not.toBe(cameraTargetKey(cameraTarget(device, device.cameras![0])))
  })

  test('changing views and focusing the exact rear camera issue no console or keyboard commands', async () => {
    const clock = () => now
    const clients = {
      console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
      keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
    }
    const log = playbackLog()
    const user = userEvent.setup()
    const { unmount } = render(<App sessionId={session} clients={clients} initialModule="live" media={log.media}
      intentDependencies={{ now: clock, nextId: () => 'camera-view-only' }} />)
    await screen.findByRole('region', { name: 'All devices' })
    act(() => clients.console.emitServer({
      v: 1, t: now + 1, type: 'state', event_id: 'six-explicit-cameras', session, roster_version: 15,
      armed: true, estop: false, selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
      capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null, accepted_plan: null, drones: fleet(),
    }))
    await user.click(screen.getByRole('button', { name: 'All cameras' }))
    expect(within(screen.getByRole('region', { name: 'All cameras' })).getAllByRole('article')).toHaveLength(6)
    await user.click(screen.getByRole('button', { name: 'Focus G-01 · Rear camera (rear)' }))
    expect(screen.getByRole('heading', { name: 'Camera inspection' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'G-01 camera' })).toHaveValue('rear')
    await waitFor(() => expect(log.sessions.at(-1)!.stream).toBe('ground1-rear'))
    await user.click(screen.getByRole('button', { name: 'Back to All cameras' }))
    await user.click(screen.getByRole('button', { name: 'All devices' }))
    expect(within(screen.getByRole('region', { name: 'All devices' })).getAllByRole('article')).toHaveLength(4)
    expect(clients.console.sent).toEqual([])
    expect(clients.keyboard.sent).toEqual([])
    unmount()
    expect(log.sessions.every((entry) => entry.close.mock.calls.length === 1)).toBe(true)
  })
})

test.each(['cameras', 'devices'] as const)('an inspected feed in %s retires on cross-device stream ambiguity and needs an explicit choice after A–B–A', async (entry) => {
  const clients = {
    console: new FixtureRelayClient(session, () => now, 'console', 'mixed'),
    keyboard: new FixtureRelayClient(session, () => now, 'keyboard', 'mixed'),
  }
  const log = playbackLog()
  const user = userEvent.setup()
  render(<App sessionId={session} clients={clients} initialModule="live" media={log.media}
    intentDependencies={{ now: () => now, nextId: () => 'camera-local-choice' }} />)
  await screen.findByRole('region', { name: 'All devices' })
  const emit = (devices: RelayAircraftState[], sequence: number) => act(() => clients.console.emitServer({
    v: 1, t: now + sequence, type: 'state', event_id: `camera-uniqueness-${sequence}`, state_sequence: sequence + 100,
    session, roster_version: 15, armed: true, estop: false, selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    pending: null, accepted_plan: null, drones: devices,
  }))
  emit(fleet(), 1)
  if (entry === 'cameras') {
    await user.click(screen.getByRole('button', { name: 'All cameras' }))
    await user.click(screen.getByRole('button', { name: 'Focus G-01 · Rear camera (rear)' }))
  } else {
    await user.click(screen.getByRole('button', { name: 'Focus G-01' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'G-01 camera' }), 'rear')
  }
  await waitFor(() => expect(log.sessions.at(-1)!.stream).toBe('ground1-rear'))
  const active = log.sessions.at(-1)!
  const sessionCount = log.sessions.length
  expect(screen.getByLabelText('Live feed G-01 · Rear camera')).toBeInTheDocument()
  const duplicate = fleet().map((device) => device.drone_id !== 12 ? device : {
    ...device, cameras: device.cameras!.map((camera) => camera.camera_id === 'rear' ? { ...camera, stream: 'ground1-rear' } : camera),
  })
  emit(duplicate, 2)
  await waitFor(() => expect(active.close).toHaveBeenCalledTimes(1))
  expect(screen.queryByLabelText('Live feed G-01 · Rear camera')).not.toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Rear camera · ambiguous stream mapping' })).toBeDisabled()
  emit(fleet(), 3)
  expect(screen.queryByLabelText('Live feed G-01 · Rear camera')).not.toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'G-01 camera' })).toHaveValue('')
  expect(log.sessions).toHaveLength(sessionCount)
  await user.selectOptions(screen.getByRole('combobox', { name: 'G-01 camera' }), 'rear')
  await waitFor(() => expect(log.sessions).toHaveLength(sessionCount + 1))
  expect(log.sessions.at(-1)!.stream).toBe('ground1-rear')
  expect(clients.console.sent).toEqual([])
  expect(clients.keyboard.sent).toEqual([])
})
