import { act, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { LivePlayer } from './LivePlayer'
import type { PlaybackDescriptor } from './playback'
import type { PlaybackSession, PlaybackStateListener, MediaPlaybackState } from './player'
import type { MediaRuntime } from './runtime'
import { RETRY_BASE_MS, RETRY_MAX_MS } from './use-playback'

const aircraft = { drone_id: 1, device_class: 'aircraft' as const, unit: 1 }
const robot = { drone_id: 11, device_class: 'ground_vehicle' as const, unit: 1 }

class ControlledSession implements PlaybackSession {
  listener?: PlaybackStateListener
  start = vi.fn(async (_video: HTMLVideoElement, _descriptor: PlaybackDescriptor, listener: PlaybackStateListener) => {
    this.listener = listener
    listener('connecting')
  })
  close = vi.fn(async () => undefined)
  emit(state: MediaPlaybackState, detail?: string) { this.listener?.(state, detail) }
}

function controlledMedia() {
  const sessions: ControlledSession[] = []
  const media: MediaRuntime = {
    configuration: {
      webrtcOrigin: 'http://localhost:8889',
      readerUsername: 'fixture-reader',
      readerPassword: 'fixture-secret',
    },
    createSession: vi.fn(() => {
      const session = new ControlledSession()
      sessions.push(session)
      return session
    }),
  }
  return { sessions, media }
}

async function advance(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

afterEach(() => vi.useRealTimers())

test('a failed aircraft tile reconnects independently, ignores old callbacks, and keeps ordinary roster updates stable', async () => {
  vi.useFakeTimers()
  const { media, sessions } = controlledMedia()
  const wall = (aircraftLive = true) => <>
    {aircraftLive && <LivePlayer key="1" device={{ ...aircraft }} media={media} />}
    <LivePlayer key="11" device={{ ...robot }} media={media} />
  </>
  const { rerender, unmount } = render(wall())
  await advance()
  expect(sessions).toHaveLength(2)
  expect(sessions[0].start.mock.calls[0][1].stream).toBe('drone1')
  expect(sessions[1].start.mock.calls[0][1].stream).toBe('ground1')
  act(() => { sessions[0].emit('playing'); sessions[1].emit('playing') })

  act(() => sessions[0].emit('failed', 'WHEP connection lost'))
  await advance()
  expect(screen.getByText('Reconnecting in 1s…')).toBeVisible()
  expect(sessions[0].close).toHaveBeenCalledTimes(1)
  expect(sessions[1].close).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Live feed G-01').parentElement).toHaveAttribute('data-playback-state', 'playing')
  await advance(RETRY_BASE_MS - 1)
  expect(sessions).toHaveLength(2)
  await advance(1)
  expect(sessions).toHaveLength(3)
  expect(sessions[2].start.mock.calls[0][1].stream).toBe('drone1')
  expect(screen.getByText('Reconnecting to the live feed…')).toBeVisible()

  act(() => { sessions[0].emit('playing'); sessions[0].emit('failed', 'obsolete failure') })
  expect(screen.getByLabelText('Live feed D-01').parentElement).toHaveAttribute('data-playback-state', 'reconnecting')
  expect(screen.queryByText(/obsolete failure/)).not.toBeInTheDocument()
  act(() => sessions[2].emit('playing'))
  expect(screen.queryByText(/Reconnecting/)).not.toBeInTheDocument()
  rerender(wall())
  await advance(RETRY_MAX_MS)
  expect(sessions).toHaveLength(3)
  expect(sessions[1].start).toHaveBeenCalledTimes(1)

  // The wall unmounts a player when that stream becomes offline.
  act(() => sessions[2].emit('failed', 'offline next'))
  await advance()
  rerender(wall(false))
  await advance(RETRY_MAX_MS)
  expect(sessions).toHaveLength(3)
  expect(sessions[2].close).toHaveBeenCalledTimes(1)
  expect(sessions[1].close).not.toHaveBeenCalled()
  unmount()
  await advance()
  expect(sessions[1].close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('retries wait for cleanup and cannot be scheduled by cleanup finishing after unmount', async () => {
  vi.useFakeTimers()
  const { media, sessions } = controlledMedia()
  const { unmount } = render(<LivePlayer device={aircraft} media={media} />)
  await advance()
  let finishCleanup!: () => void
  sessions[0].close.mockImplementation(() => new Promise<undefined>((resolve) => {
    finishCleanup = () => resolve(undefined)
  }))
  act(() => sessions[0].emit('failed', 'lost'))
  await advance(RETRY_MAX_MS * 2)
  expect(sessions).toHaveLength(1)
  unmount()
  finishCleanup()
  await advance(RETRY_MAX_MS * 2)
  expect(sessions).toHaveLength(1)
  expect(sessions[0].close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('repeated failures back off to eight seconds and stable playback resets the delay', async () => {
  vi.useFakeTimers()
  const { media, sessions } = controlledMedia()
  const { unmount } = render(<LivePlayer device={aircraft} media={media} />)
  await advance()
  for (const delay of [1_000, 2_000, 4_000, 8_000, 8_000]) {
    const count = sessions.length
    act(() => sessions[count - 1].emit('failed', 'still unavailable'))
    await advance()
    expect(screen.getByText(`Reconnecting in ${delay / 1_000}s…`)).toBeVisible()
    await advance(delay - 1)
    expect(sessions).toHaveLength(count)
    await advance(1)
    expect(sessions).toHaveLength(count + 1)
  }
  act(() => sessions.at(-1)!.emit('playing'))
  await advance(10_000)
  act(() => sessions.at(-1)!.emit('failed', 'new interruption'))
  await advance()
  expect(screen.getByText('Reconnecting in 1s…')).toBeVisible()
  unmount()
  await advance(RETRY_MAX_MS)
  expect(vi.getTimerCount()).toBe(0)
})

test('a rejected start enters the same recoverable state', async () => {
  vi.useFakeTimers()
  const { media, sessions } = controlledMedia()
  const createSession = media.createSession
  media.createSession = () => {
    const session = createSession() as ControlledSession
    session.start.mockRejectedValueOnce(new Error('browser peer unavailable'))
    return session
  }
  const { unmount } = render(<LivePlayer device={aircraft} media={media} />)
  await advance()
  expect(screen.getByText(/browser peer unavailable/)).toBeVisible()
  expect(screen.getByText('Reconnecting in 1s…')).toBeVisible()
  expect(sessions[0].close).toHaveBeenCalledTimes(1)
  unmount()
  await advance(RETRY_MAX_MS)
  expect(vi.getTimerCount()).toBe(0)
})

test('waiting for initial or foreground video frames stays visible until fresh playback is reported', async () => {
  vi.useFakeTimers()
  const { media, sessions } = controlledMedia()
  const { unmount } = render(<LivePlayer device={aircraft} media={media} />)
  await advance()
  act(() => sessions[0].emit('connecting', 'Waiting for video frames'))
  expect(screen.getByText('Waiting for video frames (drone1).')).toBeVisible()
  act(() => sessions[0].emit('playing'))
  act(() => sessions[0].emit('connecting', 'Waiting for fresh video frames'))
  expect(screen.getByText('Waiting for fresh video frames (drone1).')).toBeVisible()
  expect(screen.getByLabelText('Live feed D-01').parentElement).toHaveAttribute('data-playback-state', 'connecting')
  act(() => sessions[0].emit('playing'))
  expect(screen.queryByText(/Waiting for/)).not.toBeInTheDocument()
  expect(sessions).toHaveLength(1)
  unmount()
  await advance()
})
