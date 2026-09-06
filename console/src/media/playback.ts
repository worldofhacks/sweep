/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/playback.ts).
 * Only the WHEP request is carried over; the HLS fallback and its hls.js
 * dependency stay on #68 and are reconciled when that branch merges.
 */
import type { DeviceClass } from '../relay/contract'

export interface MediaRuntimeConfiguration {
  /** MediaMTX WebRTC origin without path, query, or credentials. */
  webrtcOrigin: string
  readerUsername: string
  readerPassword: string
}

/** What names a stream: the device's class and its unit within that class. */
export interface StreamDevice {
  device_class: DeviceClass
  unit: number
}

export interface PlaybackConfiguration extends MediaRuntimeConfiguration {
  device: StreamDevice
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
 * The console derives stream names as the relay's media monitor does:
 * `drone{unit}` for aircraft and `ground{unit}` for ground vehicles. No
 * adapter-supplied media URL is ever used.
 */
export function streamName(device: StreamDevice): string {
  return `${device.device_class === 'ground_vehicle' ? 'ground' : 'drone'}${device.unit}`
}

export function createPlaybackDescriptor(config: PlaybackConfiguration): PlaybackDescriptor {
  if (!Number.isInteger(config.device.unit) || config.device.unit < 1) {
    throw new Error('unit must be a positive integer')
  }
  if (!config.readerUsername || !config.readerPassword) {
    throw new Error('Media reader credentials are required')
  }

  const stream = streamName(config.device)
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
