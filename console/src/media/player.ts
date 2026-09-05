/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/player.ts).
 * WHEP only: the HLS fallback branch was dropped together with the hls.js
 * dependency, and a failed negotiation reports `failed` instead of throwing.
 * Reconcile against #68 when that branch merges.
 */
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

const NEGOTIATION_TIMEOUT_MS = 5_000

export class WhepPlaybackSession implements PlaybackSession {
  private readonly dependencies: PlaybackDependencies
  private peer: RTCPeerConnection | null = null
  private video: HTMLVideoElement | null = null
  private whepSessionUrl: string | null = null
  private whepAuthorization: string | null = null
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
    await this.stopCurrent()
    if (generation !== this.generation) return
    this.video = video
    onState('connecting')
    try {
      await this.negotiate(video, descriptor, generation)
      if (generation !== this.generation) return
      onState('playing')
    } catch (error) {
      if (generation !== this.generation) return
      await this.closePeer()
      resetVideo(video)
      onState('failed', error instanceof Error ? error.message : 'WHEP playback failed')
    }
  }

  async close(): Promise<void> {
    this.generation += 1
    await this.stopCurrent()
  }

  private async stopCurrent(): Promise<void> {
    await this.closePeer()
    if (this.video) resetVideo(this.video)
    this.video = null
  }

  private async negotiate(
    video: HTMLVideoElement,
    descriptor: PlaybackDescriptor,
    generation: number,
  ): Promise<void> {
    const peer = this.dependencies.createPeerConnection()
    this.peer = peer
    peer.addTransceiver('video', { direction: 'recvonly' })
    const track = waitForTrack(peer)
    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    await waitForIceGathering(peer)
    this.assertCurrent(generation)
    const response = await this.dependencies.fetcher(descriptor.primary.url, {
      method: 'POST',
      headers: {
        Authorization: descriptor.primary.authorization,
        'Content-Type': 'application/sdp',
      },
      body: peer.localDescription?.sdp ?? offer.sdp,
    })
    if (!response.ok) throw new Error(`WHEP negotiation failed with ${response.status}`)
    const location = response.headers.get('Location')
    if (!location) throw new Error('WHEP response omitted the session location')
    this.assertCurrent(generation)
    this.whepSessionUrl = new URL(location, descriptor.primary.url).toString()
    this.whepAuthorization = descriptor.primary.authorization
    await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    const event = await withTimeout(track, 'WHEP media track timed out')
    this.assertCurrent(generation)
    video.srcObject = event.streams[0] ?? new MediaStream([event.track])
    await video.play()
    await firstRenderedFrame(video)
  }

  private assertCurrent(generation: number): void {
    if (generation !== this.generation) throw new Error('Media playback was superseded')
  }

  private async closePeer(): Promise<void> {
    const peer = this.peer
    const sessionUrl = this.whepSessionUrl
    const authorization = this.whepAuthorization
    this.peer = null
    this.whepSessionUrl = null
    this.whepAuthorization = null
    if (peer) {
      peer.ontrack = null
      peer.close()
    }
    if (sessionUrl && authorization) {
      await this.dependencies
        .fetcher(sessionUrl, { method: 'DELETE', headers: { Authorization: authorization } })
        .catch(() => undefined)
    }
  }
}

function waitForTrack(peer: RTCPeerConnection): Promise<RTCTrackEvent> {
  return new Promise((resolve) => {
    peer.ontrack = (event) => {
      peer.ontrack = null
      resolve(event)
    }
  })
}

function withTimeout<T>(promise: Promise<T>, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error(message)), NEGOTIATION_TIMEOUT_MS)
    void promise.then(
      (value) => {
        window.clearTimeout(timeout)
        resolve(value)
      },
      (error: unknown) => {
        window.clearTimeout(timeout)
        reject(error)
      },
    )
  })
}

function waitForIceGathering(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      peer.removeEventListener('icegatheringstatechange', changed)
      reject(new Error('WHEP ICE gathering timed out'))
    }, NEGOTIATION_TIMEOUT_MS)
    const changed = () => {
      if (peer.iceGatheringState !== 'complete') return
      window.clearTimeout(timeout)
      peer.removeEventListener('icegatheringstatechange', changed)
      resolve()
    }
    peer.addEventListener('icegatheringstatechange', changed)
  })
}

function firstRenderedFrame(video: HTMLVideoElement): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(
      () => reject(new Error('Media frame timed out')),
      NEGOTIATION_TIMEOUT_MS,
    )
    const rendered = () => {
      window.clearTimeout(timeout)
      resolve()
    }
    if (typeof video.requestVideoFrameCallback === 'function') {
      video.requestVideoFrameCallback(rendered)
      return
    }
    video.addEventListener('loadeddata', rendered, { once: true })
  })
}

function resetVideo(video: HTMLVideoElement): void {
  video.pause()
  video.srcObject = null
  video.removeAttribute('src')
  video.load()
}
