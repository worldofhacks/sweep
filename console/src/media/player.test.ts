/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/player.test.ts)
 * and reduced to the WHEP path: the HLS fallback case is gone, and a refused
 * negotiation now reports `failed` instead of throwing. Reconcile when #68 merges.
 */
import { afterEach, expect, test, vi } from 'vitest'
import { createPlaybackDescriptor } from './playback'
import {
  DISCONNECTED_GRACE_MS, FIRST_FRAME_TIMEOUT_MS, FRAME_STALL_TIMEOUT_MS,
  NEGOTIATION_TIMEOUT_MS, WhepPlaybackSession, type MediaPlaybackState,
} from './player'

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

const descriptor = createPlaybackDescriptor({
  device: { drone_id: 1 },
  webrtcOrigin: 'http://localhost:8889',
  readerUsername: 'reader',
  readerPassword: 'secret',
})

function renderedVideo(renderFrame = true) {
  const video = document.createElement('video')
  Object.defineProperty(video, 'play', { configurable: true, value: vi.fn().mockResolvedValue(undefined) })
  Object.defineProperty(video, 'pause', { value: vi.fn() })
  Object.defineProperty(video, 'load', { value: vi.fn() })
  Object.defineProperty(video, 'requestVideoFrameCallback', {
    configurable: true,
    value: (callback: VideoFrameRequestCallback) => {
      if (renderFrame) callback(0, {} as VideoFrameCallbackMetadata)
      return 1
    },
  })
  Object.defineProperty(video, 'cancelVideoFrameCallback', { configurable: true, value: vi.fn() })
  return video
}

function fakePeer(overrides: Partial<RTCPeerConnection> = {}) {
  const events = new EventTarget()
  return {
    iceGatheringState: 'complete',
    connectionState: 'connected',
    iceConnectionState: 'connected',
    localDescription: { sdp: 'offer-sdp' },
    ontrack: null,
    addTransceiver: vi.fn(),
    createOffer: vi.fn().mockResolvedValue({ type: 'offer', sdp: 'offer-sdp' }),
    setLocalDescription: vi.fn(),
    setRemoteDescription: vi.fn(),
    addEventListener: vi.fn(events.addEventListener.bind(events)),
    removeEventListener: vi.fn(events.removeEventListener.bind(events)),
    dispatchEvent: events.dispatchEvent.bind(events),
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
  expect(states).toEqual(['connecting', 'connecting', 'playing'])

  await session.close()

  expect(fetcher).toHaveBeenLastCalledWith('http://localhost:8889/session/1', expect.objectContaining({
    method: 'DELETE',
    headers: { Authorization: descriptor.primary.authorization },
    redirect: 'error',
  }))
  expect(close).toHaveBeenCalled()
  expect(video.srcObject).toBeNull()
})

test.each([
  ['http://localhost:8889', '/session/1', 'http://localhost:8889/session/1'],
  ['http://localhost:8889', 'session/1', 'http://localhost:8889/drone1/session/1'],
  ['http://localhost:8889', '//localhost:8889/session/1', 'http://localhost:8889/session/1'],
  ['https://media.example', 'https://media.example/session/1', 'https://media.example/session/1'],
])('cleans up a same-origin WHEP session from %s with Location %s', async (origin, location, expectedUrl) => {
  const playback = createPlaybackDescriptor({
    device: { drone_id: 1 },
    webrtcOrigin: origin,
    readerUsername: 'reader',
    readerPassword: 'secret',
  })
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'DELETE'
      ? new Response('', { status: 200 })
      : new Response('answer', { status: 201, headers: { Location: location } }),
  )
  const session = new WhepPlaybackSession({ createPeerConnection: readyPeer, fetcher })
  const onState = vi.fn()

  await session.start(renderedVideo(), playback, onState)
  expect(onState).toHaveBeenLastCalledWith('playing')
  await session.close()

  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher).toHaveBeenLastCalledWith(expectedUrl, expect.objectContaining({
    method: 'DELETE',
    headers: { Authorization: playback.primary.authorization },
    redirect: 'error',
  }))
})

test.each([
  'http://other.example/session/1',
  '//other.example/session/1',
  'http://localhost:8890/session/1',
  'https://localhost:8889/session/1',
  'ftp://localhost:8889/session/1',
  'blob:http://localhost:8889/session/1',
  'http://embedded:credentials@localhost:8889/session/1',
  'http://[invalid/session/1',
])('refuses unsafe WHEP Location %s without sending cleanup credentials', async (location) => {
  const video = renderedVideo()
  const peer = fakePeer()
  const fetcher = vi.fn().mockResolvedValue(new Response('answer', {
    status: 201, headers: { Location: location },
  }))
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })
  const onState = vi.fn()

  await session.start(video, descriptor, onState)
  await session.close()

  expect(onState).toHaveBeenLastCalledWith('failed', 'WHEP response has an unsafe session location')
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledWith(descriptor.primary.url, expect.objectContaining({ method: 'POST' }))
  expect(peer.setRemoteDescription).not.toHaveBeenCalled()
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(video.srcObject).toBeNull()
})

test('a cross-origin Location arriving after close cannot receive cleanup credentials', async () => {
  let answer: (response: Response) => void = () => undefined
  const fetcher = vi.fn(() => new Promise<Response>((resolve) => { answer = resolve }))
  const peer = fakePeer()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })
  const onState = vi.fn()

  const started = session.start(renderedVideo(), descriptor, onState)
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
  await session.close()
  answer(new Response('late answer', {
    status: 201, headers: { Location: 'https://other.example/session/1' },
  }))
  await started

  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(onState.mock.calls).toEqual([['connecting']])
  expect(peer.setRemoteDescription).not.toHaveBeenCalled()
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
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher).toHaveBeenLastCalledWith('http://localhost:8889/session/9', expect.objectContaining({ method: 'DELETE' }))
  expect(video.srcObject).toBeNull()
})

function readyPeer() {
  const stream = {} as MediaStream
  const peer = fakePeer()
  peer.setRemoteDescription = vi.fn(async () => {
    peer.ontrack?.({ streams: [stream] } as unknown as RTCTrackEvent)
  })
  return peer
}

function successfulFetch() {
  return vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'DELETE'
      ? new Response('', { status: 200 })
      : new Response('answer-sdp', { status: 201, headers: { Location: '/session/1' } }),
  )
}

function connectionState(peer: RTCPeerConnection, state: RTCPeerConnectionState) {
  Object.assign(peer, { connectionState: state })
  peer.dispatchEvent(new Event('connectionstatechange'))
}

test('a connection failure after the first frame closes the peer and WHEP session exactly once', async () => {
  const video = renderedVideo()
  const peer = readyPeer()
  const fetcher = successfulFetch()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })
  await session.start(video, descriptor, onState)
  expect(onState).toHaveBeenLastCalledWith('playing')

  connectionState(peer, 'failed')
  Object.assign(peer, { iceConnectionState: 'failed' })
  peer.dispatchEvent(new Event('iceconnectionstatechange'))
  await vi.waitFor(() => expect(onState).toHaveBeenLastCalledWith('failed', 'WHEP connection lost'))

  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(video.srcObject).toBeNull()
  expect(peer.removeEventListener).toHaveBeenCalledWith('connectionstatechange', expect.any(Function))
  expect(peer.removeEventListener).toHaveBeenCalledWith('iceconnectionstatechange', expect.any(Function))
  await session.close()
  expect(peer.close).toHaveBeenCalledTimes(1)
})

test('transient disconnection can recover; a sustained disconnection fails after its grace period', async () => {
  vi.useFakeTimers()
  const peer = readyPeer()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  await session.start(renderedVideo(), descriptor, onState)

  connectionState(peer, 'disconnected')
  await vi.advanceTimersByTimeAsync(DISCONNECTED_GRACE_MS - 1)
  expect(peer.close).not.toHaveBeenCalled()
  connectionState(peer, 'connected')
  await vi.advanceTimersByTimeAsync(DISCONNECTED_GRACE_MS)
  expect(onState).toHaveBeenLastCalledWith('playing')

  connectionState(peer, 'disconnected')
  await vi.advanceTimersByTimeAsync(DISCONNECTED_GRACE_MS)
  expect(onState).toHaveBeenLastCalledWith('failed', 'WHEP connection remained disconnected')
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('a stalled POST is aborted at the negotiation deadline and a late created session is deleted', async () => {
  vi.useFakeTimers()
  const peer = fakePeer()
  const onState = vi.fn()
  let answer!: (response: Response) => void
  const fetcher = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'DELETE'
      ? Promise.resolve(new Response('', { status: 200 }))
      : new Promise<Response>((resolve) => { answer = resolve }),
  )
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })
  const started = session.start(renderedVideo(), descriptor, onState)
  await vi.advanceTimersByTimeAsync(0)
  const postSignal = fetcher.mock.calls[0][1]?.signal
  await vi.advanceTimersByTimeAsync(NEGOTIATION_TIMEOUT_MS)
  await started

  expect(postSignal?.aborted).toBe(true)
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(onState).toHaveBeenLastCalledWith('failed', 'WHEP SDP exchange timed out')
  answer(new Response('late-answer', { status: 201, headers: { Location: '/session/late' } }))
  await vi.advanceTimersByTimeAsync(0)
  expect(fetcher).toHaveBeenLastCalledWith('http://localhost:8889/session/late', expect.objectContaining({ method: 'DELETE' }))
  expect(peer.setRemoteDescription).not.toHaveBeenCalled()
  expect(onState).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})

test('local cleanup does not wait indefinitely for a failed WHEP DELETE', async () => {
  vi.useFakeTimers()
  const peer = readyPeer()
  const video = renderedVideo()
  const fetcher = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'DELETE'
      ? new Promise<Response>(() => undefined)
      : Promise.resolve(new Response('answer', { status: 201, headers: { Location: '/session/1' } })),
  )
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher })
  await session.start(video, descriptor, vi.fn())
  const closing = session.close()
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(video.srcObject).toBeNull()
  await vi.advanceTimersByTimeAsync(2_000)
  await closing
  expect(fetcher.mock.calls[1][1]?.signal?.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
})

test('closing a pending first frame cancels its callback and negotiation timeout', async () => {
  vi.useFakeTimers()
  const peer = readyPeer()
  const video = renderedVideo(false)
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(0)
  expect(video.play).toHaveBeenCalledTimes(1)
  expect(onState).toHaveBeenLastCalledWith('connecting', 'Waiting for video frames')

  await session.close()
  await started
  expect(video.cancelVideoFrameCallback).toHaveBeenCalledWith(1)
  expect(video.srcObject).toBeNull()
  expect(onState).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
})

test('an old DELETE finishing late cannot clear a replacement feed on the same video', async () => {
  const oldPeer = readyPeer()
  const newPeer = readyPeer()
  const video = renderedVideo()
  let finishDelete!: (response: Response) => void
  const oldFetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'DELETE'
      ? new Promise<Response>((resolve) => { finishDelete = resolve })
      : Promise.resolve(new Response('answer', { status: 201, headers: { Location: '/session/old' } })),
  )
  const oldSession = new WhepPlaybackSession({ createPeerConnection: () => oldPeer, fetcher: oldFetch })
  const newSession = new WhepPlaybackSession({ createPeerConnection: () => newPeer, fetcher: successfulFetch() })
  await oldSession.start(video, descriptor, vi.fn())
  const oldStream = video.srcObject
  const closing = oldSession.close()
  expect(video.srcObject).toBeNull()
  await newSession.start(video, descriptor, vi.fn())
  const newStream = video.srcObject
  expect(newStream).not.toBe(oldStream)
  expect(newStream).not.toBeNull()

  finishDelete(new Response('', { status: 200 }))
  await closing
  expect(video.srcObject).toBe(newStream)
  expect(newPeer.close).not.toHaveBeenCalled()
  await newSession.close()
})

function manuallyFramedVideo() {
  const video = renderedVideo(false)
  const callbacks = new Map<number, VideoFrameRequestCallback>()
  let nextId = 0
  Object.defineProperty(video, 'requestVideoFrameCallback', {
    value: vi.fn((callback: VideoFrameRequestCallback) => {
      const id = ++nextId
      callbacks.set(id, callback)
      return id
    }),
  })
  Object.defineProperty(video, 'cancelVideoFrameCallback', {
    value: vi.fn((id: number) => callbacks.delete(id)),
  })
  return {
    video,
    frame() {
      const waiting = [...callbacks.values()]
      callbacks.clear()
      for (const callback of waiting) callback(0, {} as VideoFrameCallbackMetadata)
    },
  }
}

test('a first keyframe arriving after 16 seconds succeeds within its separate bounded frame window', async () => {
  vi.useFakeTimers()
  const { video, frame } = manuallyFramedVideo()
  const peer = readyPeer()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(16_800)
  expect(peer.close).not.toHaveBeenCalled()
  expect(onState).toHaveBeenLastCalledWith('connecting', 'Waiting for video frames')
  frame()
  await started
  expect(onState).toHaveBeenLastCalledWith('playing')
  await session.close()
  expect(vi.getTimerCount()).toBe(0)
})

test('a missing first frame fails with a frame-specific timeout and cancels its pending callback', async () => {
  vi.useFakeTimers()
  const { video } = manuallyFramedVideo()
  const peer = readyPeer()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(FIRST_FRAME_TIMEOUT_MS)
  await started
  expect(onState).toHaveBeenLastCalledWith('failed', 'Waiting for video frames timed out')
  expect(video.cancelVideoFrameCallback).toHaveBeenCalledWith(1)
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('fresh frames reset the watchdog; three seconds of frozen video fails even with connected ICE', async () => {
  vi.useFakeTimers()
  const { video, frame } = manuallyFramedVideo()
  const peer = readyPeer()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(0)
  frame()
  await started
  await vi.advanceTimersByTimeAsync(FRAME_STALL_TIMEOUT_MS - 500)
  frame()
  await vi.advanceTimersByTimeAsync(FRAME_STALL_TIMEOUT_MS - 500)
  expect(onState).toHaveBeenLastCalledWith('playing')
  await vi.advanceTimersByTimeAsync(500)
  expect(peer.iceConnectionState).toBe('connected')
  expect(onState).toHaveBeenLastCalledWith('failed', 'Video frames stopped for 3s')
  expect(peer.close).toHaveBeenCalledTimes(1)
  frame()
  expect(onState).toHaveBeenLastCalledWith('failed', 'Video frames stopped for 3s')
  expect(vi.getTimerCount()).toBe(0)
})

test('a hidden tab is not reported fresh and requires a new frame when it becomes visible', async () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const { video, frame } = manuallyFramedVideo()
  const peer = readyPeer()
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(0)
  frame()
  await started
  visibility.mockReturnValue('hidden')
  document.dispatchEvent(new Event('visibilitychange'))
  expect(onState).toHaveBeenLastCalledWith('connecting', 'Playback paused while this tab is hidden')
  await vi.advanceTimersByTimeAsync(30_000)
  expect(peer.close).not.toHaveBeenCalled()
  visibility.mockReturnValue('visible')
  document.dispatchEvent(new Event('visibilitychange'))
  expect(onState).toHaveBeenLastCalledWith('connecting', 'Waiting for fresh video frames')
  await vi.advanceTimersByTimeAsync(1_000)
  expect(onState).toHaveBeenLastCalledWith('connecting', 'Waiting for fresh video frames')
  frame()
  expect(onState).toHaveBeenLastCalledWith('playing')
  await session.close()
  expect(vi.getTimerCount()).toBe(0)
})

test('without rVFC, loadeddata fired before play resolves is retained and frame counters detect later freezing', async () => {
  vi.useFakeTimers()
  const video = renderedVideo()
  Object.defineProperty(video, 'requestVideoFrameCallback', { value: undefined })
  let frames = 1
  let droppedFrames = 0
  Object.defineProperty(video, 'getVideoPlaybackQuality', {
    value: () => ({ totalVideoFrames: frames, droppedVideoFrames: droppedFrames }),
  })
  Object.defineProperty(video, 'play', {
    value: vi.fn(() => new Promise<void>((resolve) => {
      video.dispatchEvent(new Event('loadeddata'))
      window.setTimeout(resolve, 100)
    })),
  })
  const onState = vi.fn()
  const peer = readyPeer()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(video, descriptor, onState)
  await vi.advanceTimersByTimeAsync(100)
  await started
  expect(onState).toHaveBeenLastCalledWith('playing')
  frames += 1
  await vi.advanceTimersByTimeAsync(500)
  // Dropped frames do not make an unchanged displayed frame fresh.
  frames += 10
  droppedFrames += 10
  await vi.advanceTimersByTimeAsync(FRAME_STALL_TIMEOUT_MS - 500)
  expect(peer.close).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(500)
  expect(onState).toHaveBeenLastCalledWith('failed', 'Video frames stopped for 3s')
  expect(vi.getTimerCount()).toBe(0)
})

test('without rVFC, already available video is recognized without waiting for another loadeddata event', async () => {
  const video = renderedVideo()
  Object.defineProperties(video, {
    requestVideoFrameCallback: { value: undefined },
    readyState: { value: HTMLMediaElement.HAVE_CURRENT_DATA },
    videoWidth: { value: 640 },
    videoHeight: { value: 480 },
  })
  const onState = vi.fn()
  const session = new WhepPlaybackSession({ createPeerConnection: readyPeer, fetcher: successfulFetch() })
  await session.start(video, descriptor, onState)
  expect(onState).toHaveBeenLastCalledWith('playing')
  await session.close()
})

test.each([
  ['ICE gathering', () => fakePeer({ iceGatheringState: 'gathering' })],
  ['media track', () => fakePeer()],
] as const)('a stalled %s phase retains its own five-second deadline and error', async (phase, createPeerConnection) => {
  vi.useFakeTimers()
  const onState = vi.fn()
  const peer = createPeerConnection()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  const started = session.start(renderedVideo(), descriptor, onState)
  await vi.advanceTimersByTimeAsync(NEGOTIATION_TIMEOUT_MS)
  await started
  expect(onState).toHaveBeenLastCalledWith('failed', `WHEP ${phase} timed out`)
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('a rejected play cleans up its already-registered frame wait without leaking a callback', async () => {
  vi.useFakeTimers()
  const { video } = manuallyFramedVideo()
  Object.defineProperty(video, 'play', { value: vi.fn().mockRejectedValue(new Error('Playback permission denied')) })
  const onState = vi.fn()
  const peer = readyPeer()
  const session = new WhepPlaybackSession({ createPeerConnection: () => peer, fetcher: successfulFetch() })
  await session.start(video, descriptor, onState)
  expect(onState).toHaveBeenLastCalledWith('failed', 'Playback permission denied')
  expect(video.cancelVideoFrameCallback).toHaveBeenCalledWith(1)
  expect(peer.close).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})
