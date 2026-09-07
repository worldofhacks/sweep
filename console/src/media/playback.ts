/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/playback.ts).
 * Only the WHEP request is carried over; the HLS fallback and its hls.js
 * dependency stay on #68 and are reconciled when that branch merges.
 */
import { MAX_FLEET_DEVICES, validMediaStreamName } from '../relay/contract'

export interface MediaRuntimeConfiguration {
  /** MediaMTX WebRTC origin without path, query, or credentials. */
  webrtcOrigin: string
  readerUsername: string
  readerPassword: string
}

/** A media path is keyed by the relay's global device identity. */
export interface StreamDevice {
  drone_id: number
}

export interface PlaybackConfiguration extends MediaRuntimeConfiguration {
  device: StreamDevice
  /** Explicit camera stream provisioned by the relay; never an arbitrary URL. */
  stream?: string
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

/**
 * Media paths use the global relay ID, which matches the device-side publisher.
 */
export function streamName(device: StreamDevice): string {
  if (!Number.isInteger(device.drone_id) || device.drone_id < 1 || device.drone_id > MAX_FLEET_DEVICES) {
    throw new Error(`drone_id must be an integer from 1 through ${MAX_FLEET_DEVICES}`)
  }
  return `drone${device.drone_id}`
}

export function createPlaybackDescriptor(config: PlaybackConfiguration): PlaybackDescriptor {
  if (!config.readerUsername || !config.readerPassword) {
    throw new Error('Media reader credentials are required')
  }

  const stream = config.stream ?? streamName(config.device)
  if (!validMediaStreamName(stream)) throw new Error('Invalid configured camera stream name')
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
