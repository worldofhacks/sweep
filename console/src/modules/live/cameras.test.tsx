import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import type { MediaRuntime } from '../../media/runtime'
import type { RelayAircraftState } from '../../relay/contract'
import { fixtureAircraft } from '../../testing/fixture-relay-client'
import { FocusFeed } from './FocusFeed'
import { Mosaic } from './Mosaic'

const now = 1_756_700_000_000

function robot(unit: number): RelayAircraftState {
  return {
    ...fixtureAircraft(now)[0], drone_id: 10 + unit, unit, device_class: 'ground_vehicle',
    flight_state: null, adapter_capabilities: ['class:ground_vehicle', 'ground_drive'],
    cameras: ['front', 'rear'].map((id) => ({
      camera_id: id, label: id === 'front' ? 'Front camera' : 'Rear camera',
      stream: `robot-${unit}-${id}`, status: 'live', last_frame_at: now - 100,
    })),
  }
}

function playbackLog() {
  const started: string[] = []
  const closed: string[] = []
  const media: MediaRuntime = {
    configuration: { webrtcOrigin: 'http://media.test:8889', readerUsername: 'reader', readerPassword: 'test' },
    createSession: () => {
      let stream = ''
      return {
        async start(_video, descriptor, onState) {
          stream = descriptor.stream
          started.push(stream)
          onState('playing')
        },
        async close() { closed.push(stream) },
      }
    },
  }
  return { media, started, closed }
}

describe('configured device cameras', () => {
  test('five robots each expose two independent feeds; choosing a camera does not select or command a device', async () => {
    const log = playbackLog()
    const devices = Array.from({ length: 5 }, (_, index) => robot(index + 1))
    const onFocus = vi.fn()
    const onToggleSelection = vi.fn()
    render(<Mosaic devices={devices} now={now} focusedId={null} selection={[]}
      onFocus={onFocus} onToggleSelection={onToggleSelection} media={log.media} />)
    expect(screen.getAllByRole('article')).toHaveLength(5)
    for (let unit = 1; unit <= 5; unit += 1) {
      expect(within(screen.getByRole('combobox', { name: `G-0${unit} camera` })).getAllByRole('option')).toHaveLength(2)
    }
    await waitFor(() => expect(log.started).toHaveLength(5))
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'G-05 camera' }), 'rear')
    await waitFor(() => expect(log.started.at(-1)).toBe('robot-5-rear'))
    expect(log.closed).toEqual(['robot-5-front'])
    expect(onFocus).not.toHaveBeenCalled()
    expect(onToggleSelection).not.toHaveBeenCalled()
  })

  test('a stale camera stops independently, and reconnect or stream remapping tears down the old player', async () => {
    const log = playbackLog()
    const onFocus = vi.fn()
    const onToggleSelection = vi.fn()
    let device = robot(5)
    const view = () => <Mosaic devices={[device, robot(1)]} now={now} focusedId={null} selection={[]}
      onFocus={onFocus} onToggleSelection={onToggleSelection} media={log.media} />
    const { rerender } = render(view())
    const choose = () => screen.getByRole('combobox', { name: 'G-05 camera' })
    await userEvent.setup().selectOptions(choose(), 'rear')
    device = { ...device, cameras: device.cameras!.map((camera) => camera.camera_id === 'rear'
      ? { ...camera, last_frame_at: now - 6000 } : camera) }
    rerender(view())
    await waitFor(() => expect(log.closed).toEqual(['robot-5-front', 'robot-5-rear']))
    expect(screen.queryByLabelText('Live feed G-05 · Rear camera')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Live feed G-01 · Front camera')).toBeInTheDocument()
    expect(choose()).toHaveValue('rear')
    device = { ...device, connection_epoch: device.connection_epoch + 1 }
    rerender(view())
    expect(choose()).toHaveValue('front')
    await waitFor(() => expect(log.started.filter((stream) => stream === 'robot-5-front')).toHaveLength(2))
    device = { ...device, cameras: device.cameras!.map((camera) => camera.camera_id === 'front'
      ? { ...camera, stream: 'replacement-front' } : camera) }
    rerender(view())
    await waitFor(() => expect(log.started.at(-1)).toBe('replacement-front'))
    expect(log.closed.filter((stream) => stream === 'robot-5-front')).toHaveLength(2)
  })

  test('an explicit empty camera list never falls back to legacy live video', async () => {
    const log = playbackLog()
    const device = { ...robot(5), cameras: [] }
    const { rerender } = render(<FocusFeed focused={device} requests={[]} now={now} media={log.media} />)
    expect(screen.getByText('No cameras configured for this device.')).toBeInTheDocument()
    expect(log.started).toEqual([])
    rerender(<FocusFeed focused={{ ...device, cameras: undefined }} requests={[]} now={now} media={log.media} />)
    await waitFor(() => expect(log.started).toEqual(['drone15']))
  })

  test('focused status follows the chosen camera and device loss ends playback', async () => {
    const log = playbackLog()
    const device = robot(5)
    device.cameras![1] = { ...device.cameras![1], status: 'offline', last_frame_at: null }
    const { rerender } = render(<FocusFeed focused={device} requests={[]} now={now} media={log.media} />)
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'G-05 camera' }), 'rear')
    expect(screen.getByText('stream status').nextElementSibling).toHaveTextContent('offline')
    expect(screen.getByText('last frame').nextElementSibling).toHaveTextContent('no frame reported')
    expect(log.closed).toEqual(['robot-5-front'])
    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: 'G-05 camera' }), 'front')
    rerender(<FocusFeed focused={{ ...device, membership: 'disconnected' }} requests={[]} now={now} media={log.media} />)
    await waitFor(() => expect(log.closed).toEqual(['robot-5-front', 'robot-5-front']))
    expect(screen.getByText('stream status').nextElementSibling).toHaveTextContent('offline')
  })
})
