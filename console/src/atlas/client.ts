import { relayHttpUrl } from '../relay/origin'
import { PlatformHttp, type PlatformConnection } from '../platform/http'
import type { Capture, CaptureMetadata, GeoPosition, NewSpace, Space, SpaceDetail } from './types'

export class AtlasClient {
  readonly http: PlatformHttp
  readonly connection: PlatformConnection
  constructor(connection: PlatformConnection) {
    this.http = new PlatformHttp(connection)
    this.connection = this.http.connection
  }
  async list(signal?: AbortSignal): Promise<Space[]> {
    const result = (await this.http.request('/atlas/spaces', undefined, signal)) as {
      spaces: Space[]
    }
    if (!Array.isArray(result.spaces)) throw new Error('The space directory is unavailable.')
    return result.spaces
  }
  async create(space: NewSpace): Promise<{ space: Space; contributor_token: string }> {
    return (await this.http.request('/atlas/spaces', space)) as {
      space: Space
      contributor_token: string
    }
  }
  async detail(id: string, signal?: AbortSignal): Promise<SpaceDetail> {
    return (await this.http.request(
      `/atlas/spaces/${encodeURIComponent(id)}`,
      undefined,
      signal,
    )) as SpaceDetail
  }
  async status(id: string, status: Space['status']) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/status`, {
      status,
    })
  }
  async invitation(id: string): Promise<string> {
    const result = (await this.http.request(
      `/atlas/spaces/${encodeURIComponent(id)}/invitation`,
      {},
    )) as { contributor_token: string }
    return result.contributor_token
  }
  async reconstruct(id: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/reconstruction`, {})
  }
  async world(id: string, jobId: string, signal: AbortSignal): Promise<ArrayBuffer> {
    const response = await fetch(
      this.mediaUrl(id, `/reconstruction/${encodeURIComponent(jobId)}/cloud.glb`),
      {
        headers: { Authorization: `Bearer ${this.connection.token}` },
        signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]),
        credentials: 'omit',
        redirect: 'error',
        cache: 'no-store',
      },
    )
    if (!response.ok) throw new Error('The 3D artifact could not be loaded.')
    if (Number(response.headers.get('Content-Length')) > 16 * 1024 * 1024)
      throw new Error('The 3D artifact is too large.')
    const reader = response.body?.getReader()
    if (!reader) throw new Error('The 3D artifact is empty.')
    const chunks: Uint8Array[] = []
    let bytes = 0
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        bytes += chunk.value.length
        if (bytes > 16 * 1024 * 1024) throw new Error('The 3D artifact exceeds its size limit.')
        chunks.push(chunk.value)
      }
    } finally {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
    const data = new Uint8Array(bytes)
    let offset = 0
    for (const chunk of chunks) {
      data.set(chunk, offset)
      offset += chunk.length
    }
    return data.buffer
  }
  async request(id: string, cell_id: string, note?: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/requests`, {
      cell_id,
      ...(note ? { note } : {}),
    })
  }
  async presence(id: string, contributor_id: string, name: string, position: GeoPosition) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/presence`, {
      contributor_id,
      name,
      position,
    })
  }
  async leave(id: string, contributor_id: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/leave`, {
      contributor_id,
    })
  }
  private mediaUrl(id: string, path: string) {
    const url = relayHttpUrl(
      this.connection.baseUrl,
      `/api/sessions/${encodeURIComponent(this.connection.sessionId)}/atlas/spaces/${encodeURIComponent(id)}${path}`,
    )
    if (!url) throw new Error('Connect to a valid workspace first.')
    return url
  }
  async upload(
    id: string,
    file: File,
    metadata: CaptureMetadata,
    signal?: AbortSignal,
  ): Promise<Capture> {
    if (!file.size || file.size > 64 * 1024 * 1024)
      throw new Error('Choose a capture smaller than 64 MB.')
    const response = await fetch(this.mediaUrl(id, '/captures'), {
      method: 'POST',
      body: file,
      headers: {
        Authorization: `Bearer ${this.connection.token}`,
        'Content-Type': file.type,
        'X-Sweep-Capture': JSON.stringify(metadata).replace(
          /[\u0080-\uffff]/g,
          (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`,
        ),
      },
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(90_000)])
        : AbortSignal.timeout(90_000),
      credentials: 'omit',
      redirect: 'error',
    })
    const result = await response.json()
    if (!response.ok)
      throw new Error(
        typeof result.detail === 'string' ? result.detail : 'The upload could not be saved.',
      )
    return result as Capture
  }
  async media(id: string, captureId: string, signal: AbortSignal): Promise<Blob> {
    const response = await fetch(
      this.mediaUrl(id, `/captures/${encodeURIComponent(captureId)}/media`),
      {
        headers: { Authorization: `Bearer ${this.connection.token}` },
        signal,
        credentials: 'omit',
        redirect: 'error',
      },
    )
    if (!response.ok) throw new Error('This capture could not be loaded.')
    return response.blob()
  }
}

export function phonePosition(position: GeolocationPosition): GeoPosition {
  return {
    latitude: position.coords.latitude,
    longitude: position.coords.longitude,
    accuracy: position.coords.accuracy,
    timestamp: Math.round(position.timestamp),
    altitude: position.coords.altitude,
    heading: position.coords.heading,
  }
}

export function locate(): Promise<GeoPosition> {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Location is unavailable in this browser.'))
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) => resolve(phonePosition(position)),
      () => reject(new Error('Location could not be read. Allow location access and try again.')),
      { enableHighAccuracy: true, timeout: 15_000, maximumAge: 0 },
    )
  })
}
