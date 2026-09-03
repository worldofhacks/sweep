import Hls from 'hls.js'
import { expect, test, vi } from 'vitest'
import { MediaPlaybackSession } from './player'
import { createPlaybackDescriptor } from './playback'

const descriptor = createPlaybackDescriptor({
  droneId: 1,
  webrtcOrigin: 'http://localhost:8889',
  hlsOrigin: 'http://localhost:8888',
  readerUsername: 'reader',
  readerPassword: 'secret',
})

function renderedVideo() {
  const video = document.createElement('video')
  Object.defineProperty(video, 'play', { value: vi.fn().mockResolvedValue(undefined) })
  Object.defineProperty(video, 'pause', { value: vi.fn() })
  Object.defineProperty(video, 'load', { value: vi.fn() })
  Object.defineProperty(video, 'requestVideoFrameCallback', {
    value: (callback: VideoFrameRequestCallback) => {
      callback(0, {} as VideoFrameCallbackMetadata)
      return 1
    },
  })
  return video
}

test('negotiates WHEP SDP, renders its RTP track, and tears down the session', async () => {
  const video = renderedVideo()
  const stream = {} as MediaStream
  const close = vi.fn()
  const setLocalDescription = vi.fn()
  const setRemoteDescription = vi.fn(() => {
    window.setTimeout(() => peer.ontrack?.({ streams: [stream] } as unknown as RTCTrackEvent), 0)
    return Promise.resolve()
  })
  const peer = {
    iceGatheringState: 'complete',
    localDescription: { sdp: 'offer-sdp' },
    ontrack: null,
    addTransceiver: vi.fn(),
    createOffer: vi.fn().mockResolvedValue({ type: 'offer', sdp: 'offer-sdp' }),
    setLocalDescription,
    setRemoteDescription,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    close,
  } as unknown as RTCPeerConnection
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response('answer-sdp', { status: 201, headers: { Location: '/session/1' } }))
    .mockResolvedValueOnce(new Response('', { status: 200 }))
  const states: string[] = []
  const session = new MediaPlaybackSession({
    createPeerConnection: () => peer,
    fetcher,
    createHls: async () => {
      throw new Error('fallback should not start')
    },
  })

  await session.start(video, descriptor, (state) => states.push(state))

  expect(setLocalDescription).toHaveBeenCalledWith({ type: 'offer', sdp: 'offer-sdp' })
  expect(fetcher).toHaveBeenNthCalledWith(
    1,
    descriptor.primary.url,
    expect.objectContaining({
      method: 'POST',
      body: 'offer-sdp',
      headers: expect.objectContaining({ Authorization: descriptor.primary.authorization }),
    }),
  )
  expect(setRemoteDescription).toHaveBeenCalledWith({ type: 'answer', sdp: 'answer-sdp' })
  expect(video.srcObject).toBe(stream)
  expect(states).toEqual(['connecting', 'playing_whep'])

  await session.close()

  expect(fetcher).toHaveBeenLastCalledWith('http://localhost:8889/session/1', {
    method: 'DELETE',
    headers: { Authorization: descriptor.primary.authorization },
  })
  expect(close).toHaveBeenCalled()
  expect(video.srcObject).toBeNull()
})

test('falls back to HLS and waits for a rendered frame after WHEP failure', async () => {
  const video = renderedVideo()
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const loadSource = vi.fn(() => handlers.get(Hls.Events.MANIFEST_PARSED)?.())
  const hls = {
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    attachMedia: vi.fn(() => handlers.get(Hls.Events.MEDIA_ATTACHED)?.()),
    loadSource,
    destroy: vi.fn(),
  }
  const fetcher = vi.fn().mockResolvedValue(new Response('unavailable', { status: 503 }))
  const createHls = vi.fn(async () => ({ client: hls as unknown as Hls, events: Hls.Events }))
  const states: string[] = []
  const session = new MediaPlaybackSession({
    createPeerConnection: () => ({
      iceGatheringState: 'complete',
      localDescription: { sdp: 'offer-sdp' },
      ontrack: null,
      addTransceiver: vi.fn(),
      createOffer: vi.fn().mockResolvedValue({ type: 'offer', sdp: 'offer-sdp' }),
      setLocalDescription: vi.fn(),
      setRemoteDescription: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      close: vi.fn(),
    }) as unknown as RTCPeerConnection,
    fetcher,
    createHls,
  })

  await session.start(video, descriptor, (state) => states.push(state))

  expect(loadSource).toHaveBeenCalledWith(descriptor.fallback.url)
  expect(createHls).toHaveBeenCalledWith(descriptor.fallback.authorization)
  expect(states).toEqual(['connecting', 'whep_failed', 'playing_hls'])
  expect(video.srcObject).toBeNull()

  await session.close()
  expect(hls.destroy).toHaveBeenCalled()
})
