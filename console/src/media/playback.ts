/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/playback.ts).
 * Only the WHEP request is carried over; the HLS fallback and its hls.js
 * dependency stay on #68 and are reconciled when that branch merges.
 */

export interface MediaRuntimeConfiguration {
  /** MediaMTX WebRTC origin without path, query, or credentials. */
  webrtcOrigin: string
  readerUsername: string
  readerPassword: string
}

export interface PlaybackConfiguration extends MediaRuntimeConfiguration {
  droneId: number
}

export interface PlaybackRequest {
  protocol: 'whep'
  url: string
  authorization: string
}

export interface PlaybackDescriptor {
  stream: string
  primary: PlaybackRequest
}

/** The console derives stream names; no adapter-supplied media URL is ever used. */
export function streamName(droneId: number): string {
  return `drone${droneId}`
}

export function createPlaybackDescriptor(config: PlaybackConfiguration): PlaybackDescriptor {
  if (!Number.isInteger(config.droneId) || config.droneId < 1 || config.droneId > 6) {
    throw new Error('droneId must be an integer from 1 through 6')
  }
  if (!config.readerUsername || !config.readerPassword) {
    throw new Error('Media reader credentials are required')
  }

  const stream = streamName(config.droneId)
  const authorization = `Basic ${btoa(`${config.readerUsername}:${config.readerPassword}`)}`
  return {
    stream,
    primary: {
      protocol: 'whep',
      url: endpoint(config.webrtcOrigin, `${stream}/whep`),
      authorization,
    },
  }
}

function endpoint(origin: string, path: string): string {
  const base = new URL(origin)
  if (base.protocol !== 'http:' && base.protocol !== 'https:') {
    throw new Error('Media origins must use HTTP or HTTPS')
  }
  if (base.username || base.password) {
    throw new Error('Media origins must not contain credentials')
  }
  return new URL(path, `${base.toString().replace(/\/+$/, '')}/`).toString()
}
