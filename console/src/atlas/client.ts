import { relayHttpUrl } from '../relay/origin'
import { responseMatches } from './captureRequests'
import { BrowserSpaceDraftStore, type SpaceDraftStore } from './drafts'
import { PlatformHttp, type PlatformConnection, type PlatformFetch } from '../platform/http'
import type { Capture, CaptureMetadata, GeoPosition, NewSpace, Space, SpaceDetail, SurfaceFocus, SurfaceRegion } from './types'
import type { MemoryAnalysisRequest, MemoryAsset, MemoryContext, MemoryEdit, MemoryNotes } from '../memory/types'
import type { DateAssertion, DateRevision, Timeline } from './timeline'
import { readRemoval, type RemovalDirectory, type RemovalReceipt } from '../memory/removal'
import { AnalysisRecoveryStore } from '../memory/analysisRecovery'

export class AtlasClient {
  get memoryUploadsSupported(): boolean { return true }
  get memoryRemovalSupported(): boolean { return true }
  readonly http: PlatformHttp
  readonly connection: PlatformConnection
  private readonly fetcher: PlatformFetch
  private readonly browserDrafts: SpaceDraftStore
  private readonly browserAnalysisRecovery: AnalysisRecoveryStore
  get analysisRecovery(): AnalysisRecoveryStore { return this.browserAnalysisRecovery }
  get drafts(): SpaceDraftStore { return this.browserDrafts }
  constructor(connection: PlatformConnection, fetcher: PlatformFetch = (input, init) => globalThis.fetch(input, init)) {
    this.fetcher = fetcher
    this.http = new PlatformHttp(connection, fetcher)
    this.connection = this.http.connection
    this.browserDrafts = new BrowserSpaceDraftStore(this.connection)
    this.browserAnalysisRecovery = new AnalysisRecoveryStore(['browser', connection.baseUrl, connection.sessionId, connection.token])
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
  async timeline(id: string, signal?: AbortSignal): Promise<Timeline> {
    const result = await this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/timeline`, undefined, signal) as Timeline
    if (!Array.isArray(result.entries) || result.entries.length > 500 || result.entries.some(entry => !entry.capture?.id || !entry.time || !Array.isArray(entry.evidence) || !Array.isArray(entry.warnings)))
      throw new Error('The timeline could not be read. Your original captures are unchanged.')
    return result
  }
  async dateHistory(space: string, capture: string, signal?: AbortSignal): Promise<DateRevision[]> {
    const result = await this.http.request(`/atlas/spaces/${encodeURIComponent(space)}/captures/${encodeURIComponent(capture)}/date`, undefined, signal) as { history: DateRevision[] }
    if (!Array.isArray(result.history)) throw new Error('Date history is unavailable.')
    return result.history
  }
  async correctDate(space: string, capture: string, revision: number, assertion: DateAssertion): Promise<DateRevision> {
    return await this.http.request(`/atlas/spaces/${encodeURIComponent(space)}/captures/${encodeURIComponent(capture)}/date`, { revision, assertion }) as DateRevision
  }
  async invitation(id: string): Promise<string> {
    const result = (await this.http.request(
      `/atlas/spaces/${encodeURIComponent(id)}/invitation`,
      {},
    )) as { contributor_token: string }
    return result.contributor_token
  }
  async accountInvitations(id: string, signal?: AbortSignal): Promise<{ enabled: boolean; invitations: AccountInvitation[] }> {
    return await this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/account-invitations`, undefined, signal) as { enabled: boolean; invitations: AccountInvitation[] }
  }
  async inviteAccount(id: string, role: 'viewer' | 'contributor', lifetime_hours: number): Promise<AccountInvitation & { token: string }> {
    return await this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/account-invitations`, { role, lifetime_hours }) as AccountInvitation & { token: string }
  }
  async revokeAccountInvitation(id: string, invitation: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/account-invitations/${encodeURIComponent(invitation)}`, undefined, undefined, 10_000, 'DELETE')
  }
  async members(id: string, signal?: AbortSignal): Promise<{ members: SpaceMember[] }> {
    return await this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/members`, undefined, signal) as { members: SpaceMember[] }
  }
  async removeMember(id: string, account: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/members/${encodeURIComponent(account)}`, undefined, undefined, 10_000, 'DELETE')
  }
  async reconstruct(id: string) {
    return this.http.request(`/atlas/spaces/${encodeURIComponent(id)}/reconstruction`, {})
  }
  async memory(space: string, capture: string, signal?: AbortSignal): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture), undefined, signal) as MemoryContext
  }
  async removal(space: string, capture: string, signal?: AbortSignal) {
    const result = readRemoval(await this.http.request(this.memoryPath(space, capture).replace(/\/memory$/, '/removal'), undefined, signal), capture)
    if (result.state !== 'preview') await this.retireRemovedRecovery(space, capture)
    return result
  }
  async removeCapture(space: string, capture: string, confirmation: string): Promise<RemovalReceipt> {
    if (!this.memoryRemovalSupported) throw new Error('Use the web console to remove a memory.')
    const result = readRemoval(await this.http.request(this.memoryPath(space, capture).replace(/\/memory$/, '/removal'), { confirmation }), capture)
    if (result.state === 'preview') throw new Error('Removal was not confirmed. Check its status before trying again.')
    await this.retireRemovedRecovery(space, capture)
    return result
  }
  async removals(space: string, signal?: AbortSignal, before?: number): Promise<RemovalDirectory> {
    if (before !== undefined && (!Number.isSafeInteger(before) || before < 1 || before > 1001)) throw new Error('Choose an existing receipt page.')
    const result = await this.http.request(`/atlas/spaces/${encodeURIComponent(space)}/removals${before === undefined ? '' : `/${before}`}`, undefined, signal) as RemovalDirectory
    if (!result || !Array.isArray(result.receipts) || result.receipts.length > 20 || !['space', 'own'].includes(result.scope) || !Number.isSafeInteger(result.pending) || result.pending < 0 || !Number.isSafeInteger(result.completed) || result.completed < 0 || result.pending + result.completed > 1000 || !(result.next_before === null || Number.isSafeInteger(result.next_before) && result.next_before > 0 && result.next_before <= 1000 && (before === undefined || result.next_before < before)))
      throw new Error('Removal receipts could not be verified.')
    result.receipts.forEach(item => { if (readRemoval(item).state === 'preview') throw new Error('Removal receipts could not be verified.') })
    await Promise.all(result.receipts.map(item => this.retireRemovedRecovery(space, item.capture_id)))
    return result
  }
  private async retireRemovedRecovery(space: string, capture: string) {
    // A receipt proves source withdrawal, so this reference can no longer admit work.
    // Browser storage failure must not turn a confirmed relay removal into an unknown write.
    // Relay receipts do not claim erasure of unavailable browser/device storage.
    await this.analysisRecovery.forgetRemoved(space, capture).catch(() => {})
  }
  async memoryHistory(space: string, capture: string, signal?: AbortSignal, before?: number): Promise<MemoryEdit[]> {
    if (before !== undefined && (!Number.isInteger(before) || before < 1 || before > 201)) throw new Error('Choose an existing history page.')
    const value = await this.http.request(this.memoryPath(space, capture) + '/history' + (before === undefined ? '' : `/${before}`), undefined, signal) as { history: MemoryEdit[] }
    if (!Array.isArray(value.history)) throw new Error('Memory history is unavailable.')
    return value.history
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
  async analyzeMemory(space: string, capture: string, options: MemoryAnalysisRequest): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture) + '/analyze', options) as MemoryContext
  }
  async cancelMemory(space: string, capture: string, analysis_id: string): Promise<MemoryContext> {
    return await this.http.request(this.memoryPath(space, capture) + '/cancel', { analysis_id }) as MemoryContext
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

export interface AccountInvitation { id: string; role: 'viewer' | 'contributor'; expires_at: number }
export interface SpaceMember { account_id: string; role: 'viewer' | 'contributor' | 'owner'; joined_at: number }

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
