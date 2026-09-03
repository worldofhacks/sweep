export interface MediaRuntimeConfiguration {
  webrtcOrigin: string
  hlsOrigin: string
  readerUsername: string
  readerPassword: string
}

export interface PlaybackConfiguration extends MediaRuntimeConfiguration {
  droneId: number
}

export interface PlaybackRequest {
  protocol: 'whep' | 'hls'
  url: string
  authorization: string
}

export interface PlaybackDescriptor {
  stream: string
  primary: PlaybackRequest
  fallback: PlaybackRequest
}

export function createPlaybackDescriptor(config: PlaybackConfiguration): PlaybackDescriptor {
  if (!Number.isInteger(config.droneId) || config.droneId < 1 || config.droneId > 6) {
    throw new Error('droneId must be an integer from 1 through 6')
  }
  if (!config.readerUsername || !config.readerPassword) {
    throw new Error('Media reader credentials are required')
  }

  const stream = `drone${config.droneId}`
  const authorization = `Basic ${btoa(`${config.readerUsername}:${config.readerPassword}`)}`
  return {
    stream,
    primary: {
      protocol: 'whep',
      url: endpoint(config.webrtcOrigin, `${stream}/whep`),
      authorization,
    },
    fallback: {
      protocol: 'hls',
      url: endpoint(config.hlsOrigin, `${stream}/index.m3u8`),
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
