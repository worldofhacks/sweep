import type { PlaybackDescriptor } from './playback'

export type MediaPlaybackState = 'connecting' | 'playing' | 'failed'
export type PlaybackStateListener = (state: MediaPlaybackState, detail?: string) => void

/** What the Live module needs from a playback session; tests inject a fake. */
export interface PlaybackSession {
  start(
    video: HTMLVideoElement,
    descriptor: PlaybackDescriptor,
    onState: PlaybackStateListener,
  ): Promise<void>
  close(): Promise<void>
}

export interface PlaybackDependencies {
  createPeerConnection: () => RTCPeerConnection
  fetcher: typeof fetch
}

const browserDependencies: PlaybackDependencies = {
  createPeerConnection: () => new RTCPeerConnection(),
  fetcher: (input, init) => fetch(input, init),
}

export const NEGOTIATION_TIMEOUT_MS = 5_000
// Current 15 fps publishers may use x264's default 250-frame GOP (~16.7 s).
// Initial keyframe acquisition has a separate budget from an already-playing feed.
export const FIRST_FRAME_TIMEOUT_MS = 20_000
export const FRAME_STALL_TIMEOUT_MS = 3_000
const FRAME_CHECK_INTERVAL_MS = 500
export const DISCONNECTED_GRACE_MS = 2_000
const DELETE_TIMEOUT_MS = 2_000

interface PlaybackAttempt {
  generation: number
  peer: RTCPeerConnection
  video: HTMLVideoElement
  abort: AbortController
  sessionUrl: string | null
  authorization: string
  cleanup: Array<() => void>
  disposal?: Promise<void>
}

export class WhepPlaybackSession implements PlaybackSession {
  private readonly dependencies: PlaybackDependencies
  private current: PlaybackAttempt | null = null
  private generation = 0

  constructor(dependencies: PlaybackDependencies = browserDependencies) {
    this.dependencies = dependencies
  }

  async start(
    video: HTMLVideoElement,
    descriptor: PlaybackDescriptor,
    onState: PlaybackStateListener,
  ): Promise<void> {
    const generation = ++this.generation
    const previous = this.current
    this.current = null
    if (previous) await this.dispose(previous)
    if (generation !== this.generation) return
    onState('connecting')
    let attempt: PlaybackAttempt | undefined
    try {
      attempt = {
        generation,
        peer: this.dependencies.createPeerConnection(),
        video,
        abort: new AbortController(),
        sessionUrl: null,
        authorization: descriptor.primary.authorization,
        cleanup: [],
      }
      this.current = attempt
      this.monitorConnection(attempt, onState)
      await this.negotiate(attempt, descriptor, onState)
      if (this.isCurrent(attempt)) {
        onState('playing')
        this.monitorFrames(attempt, onState)
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'WHEP playback failed'
      if (attempt) await this.fail(attempt, detail, onState)
      else if (generation === this.generation) onState('failed', detail)
    }
  }

  async close(): Promise<void> {
    this.generation += 1
    const attempt = this.current
    this.current = null
    if (attempt) await this.dispose(attempt)
  }

  private isCurrent(attempt: PlaybackAttempt): boolean {
    return this.current === attempt && this.generation === attempt.generation && !attempt.abort.signal.aborted
  }

  private assertCurrent(attempt: PlaybackAttempt): void {
    if (!this.isCurrent(attempt)) throw new Error('Media playback was superseded')
  }

  private async fail(
    attempt: PlaybackAttempt,
    detail: string,
    onState: PlaybackStateListener,
  ): Promise<void> {
    if (!this.isCurrent(attempt)) return
    this.current = null
    await this.dispose(attempt)
    if (this.generation === attempt.generation) onState('failed', detail)
  }

  private monitorConnection(attempt: PlaybackAttempt, onState: PlaybackStateListener): void {
    const { peer } = attempt
    let disconnectedTimer: number | undefined
    const clearDisconnectedTimer = () => {
      window.clearTimeout(disconnectedTimer)
      disconnectedTimer = undefined
    }
    const changed = () => {
      if (!this.isCurrent(attempt)) return
      const states = [peer.connectionState, peer.iceConnectionState]
      if (states.some((state) => state === 'failed' || state === 'closed')) {
        clearDisconnectedTimer()
        void this.fail(attempt, 'WHEP connection lost', onState)
      } else if (states.includes('disconnected')) {
        // ICE can recover briefly interrupted Wi-Fi without replacing a healthy peer.
        disconnectedTimer ??= window.setTimeout(() => {
          disconnectedTimer = undefined
          void this.fail(attempt, 'WHEP connection remained disconnected', onState)
        }, DISCONNECTED_GRACE_MS)
      } else {
        clearDisconnectedTimer()
      }
    }
    peer.addEventListener('connectionstatechange', changed)
    peer.addEventListener('iceconnectionstatechange', changed)
    attempt.cleanup.push(() => {
      clearDisconnectedTimer()
      peer.removeEventListener('connectionstatechange', changed)
      peer.removeEventListener('iceconnectionstatechange', changed)
    })
  }

  private monitorFrames(attempt: PlaybackAttempt, onState: PlaybackStateListener): void {
    const { video } = attempt
    let lastFrameAt = Date.now()
    let frameId: number | undefined
    let frameGeneration = 0
    let awaitingFresh = false
    const supportsFrameCallback = typeof video.requestVideoFrameCallback === 'function'
    const progress = () => {
      if (typeof video.getVideoPlaybackQuality !== 'function') return video.currentTime
      const quality = video.getVideoPlaybackQuality()
      return quality.totalVideoFrames - quality.droppedVideoFrames
    }
    let lastProgress = progress()
    const receivedFrame = () => {
      if (!this.isCurrent(attempt) || document.visibilityState === 'hidden') return
      lastFrameAt = Date.now()
      if (awaitingFresh) {
        awaitingFresh = false
        onState('playing')
      }
    }
    const requestFrame = () => {
      if (!supportsFrameCallback || frameId !== undefined) return
      const generation = frameGeneration
      // The flag also handles synchronous test callbacks without retaining a finished request.
      let delivered = false
      const id = video.requestVideoFrameCallback(() => {
        delivered = true
        if (generation !== frameGeneration || !this.isCurrent(attempt)) return
        frameId = undefined
        receivedFrame()
      })
      if (!delivered) frameId = id
    }
    const cancelFrame = () => {
      frameGeneration += 1
      if (frameId !== undefined) video.cancelVideoFrameCallback?.(frameId)
      frameId = undefined
    }
    const visibilityChanged = () => {
      if (!this.isCurrent(attempt)) return
      cancelFrame()
      lastFrameAt = Date.now()
      lastProgress = progress()
      awaitingFresh = true
      onState('connecting', document.visibilityState === 'hidden'
        ? 'Playback paused while this tab is hidden'
        : 'Waiting for fresh video frames')
      if (document.visibilityState !== 'hidden') requestFrame()
    }
    const interval = window.setInterval(() => {
      if (!this.isCurrent(attempt) || document.visibilityState === 'hidden') return
      if (!supportsFrameCallback) {
        const nextProgress = progress()
        if (nextProgress !== lastProgress) {
          lastProgress = nextProgress
          receivedFrame()
        }
      }
      if (Date.now() - lastFrameAt >= FRAME_STALL_TIMEOUT_MS) {
        void this.fail(attempt, 'Video frames stopped for 3s', onState)
      } else {
        requestFrame()
      }
    }, FRAME_CHECK_INTERVAL_MS)
    document.addEventListener('visibilitychange', visibilityChanged)
    attempt.cleanup.push(() => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', visibilityChanged)
      cancelFrame()
    })
    if (document.visibilityState === 'hidden') visibilityChanged()
    else requestFrame()
  }

  private async negotiate(
    attempt: PlaybackAttempt,
    descriptor: PlaybackDescriptor,
    onState: PlaybackStateListener,
  ): Promise<void> {
    const { peer, video, abort } = attempt
    peer.addTransceiver('video', { direction: 'recvonly' })
    const track = waitForTrack(peer, abort.signal)
    // Cancellation can happen while another negotiation step is still pending.
    void track.catch(() => undefined)
    const offer = await bounded(
      this.createOffer(attempt), NEGOTIATION_TIMEOUT_MS, 'WHEP ICE gathering timed out', abort.signal,
    )
    const sdp = await bounded(
      this.exchangeAnswer(attempt, descriptor, offer),
      NEGOTIATION_TIMEOUT_MS, 'WHEP SDP exchange timed out', abort.signal,
    )
    const event = await bounded((async () => {
      this.assertCurrent(attempt)
      await peer.setRemoteDescription({ type: 'answer', sdp })
      return track
    })(), NEGOTIATION_TIMEOUT_MS, 'WHEP media track timed out', abort.signal)
    this.assertCurrent(attempt)
    video.srcObject = event.streams[0] ?? new MediaStream([event.track])
    onState('connecting', 'Waiting for video frames')
    // Install loadeddata/rVFC before play(): play may resolve after loadeddata has fired.
    const frame = firstRenderedFrame(video, abort.signal)
    await bounded(Promise.all([
      frame,
      Promise.resolve().then(() => { this.assertCurrent(attempt); return video.play() }),
    ]), FIRST_FRAME_TIMEOUT_MS, 'Waiting for video frames timed out', abort.signal)
  }

  private async createOffer(attempt: PlaybackAttempt): Promise<RTCSessionDescriptionInit> {
    const { peer, abort } = attempt
    const offer = await peer.createOffer()
    this.assertCurrent(attempt)
    await peer.setLocalDescription(offer)
    this.assertCurrent(attempt)
    await waitForIceGathering(peer, abort.signal)
    this.assertCurrent(attempt)
    return offer
  }

  private async exchangeAnswer(
    attempt: PlaybackAttempt,
    descriptor: PlaybackDescriptor,
    offer: RTCSessionDescriptionInit,
  ): Promise<string> {
    const response = await this.dependencies.fetcher(descriptor.primary.url, {
      method: 'POST',
      headers: {
        Authorization: attempt.authorization,
        'Content-Type': 'application/sdp',
      },
      body: attempt.peer.localDescription?.sdp ?? offer.sdp,
      signal: attempt.abort.signal,
    }).then((response) => {
      const location = response.ok ? response.headers.get('Location') : null
      if (location) {
        const sessionUrl = resolveSessionLocation(location, descriptor.primary.url)
        if (this.isCurrent(attempt)) attempt.sessionUrl = sessionUrl
        // A server may create its session just before the POST is cancelled.
        else void this.deleteSession(sessionUrl, attempt.authorization)
      }
      return response
    })
    this.assertCurrent(attempt)
    if (!response.ok) throw new Error(`WHEP negotiation failed with ${response.status}`)
    if (!attempt.sessionUrl) throw new Error('WHEP response omitted the session location')
    const sdp = await response.text()
    this.assertCurrent(attempt)
    return sdp
  }

  private dispose(attempt: PlaybackAttempt): Promise<void> {
    if (attempt.disposal) return attempt.disposal
    attempt.abort.abort()
    for (const cleanup of attempt.cleanup) cleanup()
    attempt.peer.ontrack = null
    attempt.peer.close()
    // Release the shared video synchronously, before an asynchronous DELETE can finish late.
    resetVideo(attempt.video)
    attempt.disposal = attempt.sessionUrl
      ? this.deleteSession(attempt.sessionUrl, attempt.authorization)
      : Promise.resolve()
    return attempt.disposal
  }

  private async deleteSession(sessionUrl: string, authorization: string): Promise<void> {
    const abort = new AbortController()
    try {
      await bounded(
        this.dependencies.fetcher(sessionUrl, {
          method: 'DELETE',
          headers: { Authorization: authorization },
          redirect: 'error',
          signal: abort.signal,
        }),
        DELETE_TIMEOUT_MS,
        'WHEP cleanup timed out',
      )
    } catch {
      // Local cleanup must still complete when the media server is unavailable.
    } finally {
      abort.abort()
    }
  }
}

function resolveSessionLocation(location: string, endpoint: string): string {
  try {
    const session = new URL(location, endpoint)
    if ((session.protocol === 'http:' || session.protocol === 'https:')
      && session.origin === new URL(endpoint).origin
      && !session.username && !session.password) {
      return session.toString()
    }
  } catch {
    // A malformed server-provided URL must not escape into error details.
  }
  throw new Error('WHEP response has an unsafe session location')
}

function bounded<T>(promise: Promise<T>, timeoutMs: number, message: string, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.clearTimeout(timeout)
      signal?.removeEventListener('abort', cancelled)
    }
    const cancelled = () => {
      cleanup()
      reject(new Error('Media playback was superseded'))
    }
    const timeout = window.setTimeout(() => {
      cleanup()
      reject(new Error(message))
    }, timeoutMs)
    signal?.addEventListener('abort', cancelled, { once: true })
    if (signal?.aborted) cancelled()
    void promise.then(
      (value) => { cleanup(); resolve(value) },
      (error: unknown) => { cleanup(); reject(error) },
    )
  })
}

function waitForTrack(peer: RTCPeerConnection, signal: AbortSignal): Promise<RTCTrackEvent> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      peer.ontrack = null
      signal.removeEventListener('abort', cancelled)
    }
    const cancelled = () => {
      cleanup()
      reject(new Error('Media playback was superseded'))
    }
    peer.ontrack = (event) => { cleanup(); resolve(event) }
    signal.addEventListener('abort', cancelled, { once: true })
    if (signal.aborted) cancelled()
  })
}

function waitForIceGathering(peer: RTCPeerConnection, signal: AbortSignal): Promise<void> {
  if (peer.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      peer.removeEventListener('icegatheringstatechange', changed)
      signal.removeEventListener('abort', cancelled)
    }
    const changed = () => {
      if (peer.iceGatheringState !== 'complete') return
      cleanup()
      resolve()
    }
    const cancelled = () => {
      cleanup()
      reject(new Error('Media playback was superseded'))
    }
    peer.addEventListener('icegatheringstatechange', changed)
    signal.addEventListener('abort', cancelled, { once: true })
    if (signal.aborted) cancelled()
  })
}

function firstRenderedFrame(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let frameId: number | undefined
    const cleanup = () => {
      if (frameId !== undefined) video.cancelVideoFrameCallback?.(frameId)
      video.removeEventListener('loadeddata', rendered)
      signal.removeEventListener('abort', cancelled)
    }
    const rendered = () => { cleanup(); resolve() }
    const cancelled = () => {
      cleanup()
      reject(new Error('Media playback was superseded'))
    }
    signal.addEventListener('abort', cancelled, { once: true })
    if (signal.aborted) { cancelled(); return }
    if (typeof video.requestVideoFrameCallback === 'function') {
      frameId = video.requestVideoFrameCallback(rendered)
    } else {
      video.addEventListener('loadeddata', rendered, { once: true })
      if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0 && video.videoHeight > 0) rendered()
    }
  })
}

function resetVideo(video: HTMLVideoElement): void {
  video.pause()
  video.srcObject = null
  video.removeAttribute('src')
  video.load()
}
