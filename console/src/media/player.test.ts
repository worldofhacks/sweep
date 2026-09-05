/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/player.test.ts)
 * and reduced to the WHEP path: the HLS fallback case is gone, and a refused
 * negotiation now reports `failed` instead of throwing. Reconcile when #68 merges.
 */
import { expect, test, vi } from 'vitest'
import { createPlaybackDescriptor } from './playback'
import { WhepPlaybackSession, type MediaPlaybackState } from './player'

const descriptor = createPlaybackDescriptor({
  droneId: 1,
  webrtcOrigin: 'http://localhost:8889',
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

function fakePeer(overrides: Partial<RTCPeerConnection> = {}) {
  return {
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
    ...overrides,
  } as unknown as RTCPeerConnection
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
  const peer = fakePeer({ setLocalDescription, setRemoteDescription, close })
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response('answer-sdp', { status: 201, headers: { Location: '/session/1' } }))
    .mockResolvedValueOnce(new Response('', { status: 200 }))
  const states: MediaPlaybackState[] = []
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })

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
  expect(states).toEqual(['connecting', 'playing'])

  await session.close()

  expect(fetcher).toHaveBeenLastCalledWith('http://localhost:8889/session/1', {
    method: 'DELETE',
    headers: { Authorization: descriptor.primary.authorization },
  })
  expect(close).toHaveBeenCalled()
  expect(video.srcObject).toBeNull()
})

test('reports failed with the refusal detail instead of throwing or falling back', async () => {
  const video = renderedVideo()
  const peer = fakePeer()
  const fetcher = vi.fn().mockResolvedValue(new Response('unavailable', { status: 503 }))
  const states: Array<[MediaPlaybackState, string | undefined]> = []
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })

  await expect(
    session.start(video, descriptor, (state, detail) => states.push([state, detail])),
  ).resolves.toBeUndefined()

  expect(states).toEqual([
    ['connecting', undefined],
    ['failed', 'WHEP negotiation failed with 503'],
  ])
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(peer.close).toHaveBeenCalled()
  expect(video.srcObject).toBeNull()
})

test('a close during negotiation supersedes the session so no late state is reported', async () => {
  const video = renderedVideo()
  const peer = fakePeer()
  let answer: (response: Response) => void = () => undefined
  const fetcher = vi.fn(
    (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === 'DELETE'
        ? Promise.resolve(new Response('', { status: 200 }))
        : new Promise<Response>((resolve) => {
            answer = resolve
          }),
  ) as unknown as typeof fetch
  const states: MediaPlaybackState[] = []
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })

  const started = session.start(video, descriptor, (state) => states.push(state))
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
  await session.close()
  answer(new Response('answer-sdp', { status: 201, headers: { Location: '/session/9' } }))
  await started

  expect(states).toEqual(['connecting'])
  expect(peer.close).toHaveBeenCalled()
  expect(peer.setRemoteDescription).not.toHaveBeenCalled()
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(video.srcObject).toBeNull()
})
