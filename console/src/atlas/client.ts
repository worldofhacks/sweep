import { relayHttpUrl } from '../relay/origin'
import { responseMatches } from './captureRequests'
import { BrowserSpaceDraftStore, type SpaceDraftStore } from './drafts'
import { PlatformHttp, type PlatformConnection, type PlatformFetch } from '../platform/http'
import type { Capture, CaptureMetadata, GeoPosition, NewSpace, Space, SpaceDetail, SurfaceFocus, SurfaceRegion } from './types'
import type { MemoryAsset, MemoryContext, MemoryNotes } from '../memory/types'

export class AtlasClient {
  get memoryUploadsSupported(): boolean { return true }
  readonly http: PlatformHttp
  readonly connection: PlatformConnection
  private readonly fetcher: PlatformFetch
  private readonly browserDrafts: SpaceDraftStore
  get drafts(): SpaceDraftStore { return this.browserDrafts }
  constructor(connection: PlatformConnection, fetcher: PlatformFetch = (input, init) => globalThis.fetch(input, init)) {
    this.fetcher = fetcher
    this.http = new PlatformHttp(connection, fetcher)
    this.connection = this.http.connection
    this.browserDrafts = new BrowserSpaceDraftStore(this.connection)
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
  async publishDraft(id: string, space: NewSpace): Promise<{ space: Space; contributor_token: string | null }> {
    const result = await this.http.request(`/atlas/spaces/drafts/${encodeURIComponent(id)}/publish`, space) as {
      draft_id: string; space: Space; contributor_token: string | null
    }
    if (result.draft_id !== id || !result.space?.id) throw new Error('Publication was not confirmed. Retry this draft to check without duplicating it.')
    return result
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
  async memory(space: string, capture: string, signal?: AbortSignal): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture), undefined, signal) as MemoryContext
  }
  async saveMemory(space: string, capture: string, revision: number, notes: MemoryNotes): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture), { revision, notes }) as MemoryContext
  }
  async inspectMemory(space: string, capture: string, signal?: AbortSignal): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture) + '/inspect', {}, signal, 25_000) as MemoryContext
  }
  async reviewMemory(space: string, capture: string, revision: number, analysis_id: string | null): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture) + '/review', { revision, analysis_id }) as MemoryContext
  }
  async analyzeMemory(space: string, capture: string, options: { revision: number; weather: boolean; ai: boolean; audio_asset_id: string | null }): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture) + '/analyze', options) as MemoryContext
  }
  private memoryPath(space: string, capture: string) {
    return `/atlas/spaces/${encodeURIComponent(space)}/captures/${encodeURIComponent(capture)}/memory`
  }
  async uploadMemoryAsset(space: string, capture: string, file: File, metadata: Pick<MemoryAsset, 'title' | 'role' | 'rights_confirmed'>, signal: AbortSignal): Promise<MemoryAsset> {
    if (!this.memoryUploadsSupported) throw new Error('Use the web console to attach a memory recording.')
    if (!file.size || file.size > 64 * 1024 * 1024) throw new Error('Choose a recording smaller than 64 MB.')
    const response = await this.fetcher(this.mediaUrl(space, `/captures/${encodeURIComponent(capture)}/memory/assets`), {
      method: 'POST', body: file, credentials: 'omit', redirect: 'error',
      signal: AbortSignal.any([signal, AbortSignal.timeout(90_000)]),
      headers: { Authorization: `Bearer ${this.connection.token}`, 'Content-Type': file.type,
        'X-Sweep-Memory-Asset': JSON.stringify(metadata).replace(/[\u0080-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`) },
    })
    const result = await response.json()
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Recording upload failed.')
    return result as MemoryAsset
  }
  async memoryAssetMedia(space: string, capture: string, asset: string, signal: AbortSignal): Promise<Blob> {
    return this.readMedia(space, `/captures/${encodeURIComponent(capture)}/memory/assets/${encodeURIComponent(asset)}/media`, signal)
  }
  async world(id: string, jobId: string, signal: AbortSignal): Promise<ArrayBuffer> {
    const response = await this.fetcher(
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
  async requestSurface(id: string, focus: SurfaceFocus, note: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/surface-requests`, {
      job_id: focus.job_id, artifact_sha256: focus.artifact_sha256, region_id: focus.region.id, note,
    })
  }
  async surfaceRegion(id: string, jobId: string, checksum: string, regionId: string, signal: AbortSignal): Promise<SurfaceRegion> {
    const manifest = await this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/reconstruction/${encodeURIComponent(jobId)}/manifest.json`, undefined, signal) as {
      job_id: string; artifact_sha256: string; surface_review?: { regions: SurfaceRegion[] }
    }
    const region = manifest.surface_review?.regions.find(item => item.id === regionId)
    if (manifest.job_id !== jobId || manifest.artifact_sha256 !== checksum || !region?.segments?.length)
      throw new Error('This region could not be verified against the displayed build. Refresh and try again.')
    return region
  }
  async dismissSurfaceRequest(id: string, jobId: string, regionId: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/surface-requests/${encodeURIComponent(jobId)}/${encodeURIComponent(regionId)}/dismiss`, {})
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
    const response = await this.fetcher(this.mediaUrl(id, '/captures'), {
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
    if (metadata.response_to && !responseMatches(metadata.response_to, result.response_to))
      throw new Error('The upload was not confirmed against this request. Keep the original and retry.')
    return result as Capture
  }
  async media(id: string, captureId: string, signal: AbortSignal): Promise<Blob> {
    return this.readMedia(id, `/captures/${encodeURIComponent(captureId)}/media`, signal)
  }
  private async readMedia(id: string, path: string, signal: AbortSignal): Promise<Blob> {
    const response = await this.fetcher(
      this.mediaUrl(id, path),
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
