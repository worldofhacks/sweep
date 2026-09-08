import { equalNavigationEvidence, navigationPreviewValidity, parseNavigationCatalog, parseNavigationPreview, type NavigationCatalog, type NavigationClient, type NavigationPreview, type NavigationPreviewRequest, type NavigationSnapshot } from '../navigation'
import { isRecord, PlatformHttp } from './http'

/** A response cannot gain lifetime from network delay or the browser's clock offset. */
function rebase(raw: unknown, serverNow: unknown, started: number, ended: number): unknown {
  if (!isRecord(raw) || typeof serverNow !== 'number' || !Number.isSafeInteger(serverNow)
    || typeof raw.receivedAt !== 'number' || typeof raw.expiresAt !== 'number'
    || !Number.isSafeInteger(raw.receivedAt) || !Number.isSafeInteger(raw.expiresAt)
    || raw.receivedAt > serverNow || ended < started) throw new Error('The relay returned invalid navigation timestamps.')
  const remaining = raw.expiresAt - serverNow - (ended - started)
  if (remaining <= 0 || remaining > 60_000) throw new Error('Navigation evidence expired before it arrived.')
  return { ...raw, receivedAt: ended, expiresAt: ended + remaining }
}

export class HttpNavigationClient implements NavigationClient {
  private snapshot: NavigationSnapshot = Object.freeze({ status: 'loading', reason: 'Loading accepted destinations…', catalog: null, preview: null, reviewSupported: true })
  private listeners = new Set<(value: NavigationSnapshot) => void>()
  private generation = 0
  private refreshTimer: ReturnType<typeof setTimeout> | undefined
  private refreshRequest: AbortController | undefined
  private confirmation = new Map<string, { preview: NavigationPreview; hash: string }>()
  private previewSequence = 0

  private readonly http: PlatformHttp
  private readonly now: () => number
  constructor(http: PlatformHttp, now = Date.now) { this.http = http; this.now = now }
  getSnapshot() { return this.snapshot }
  subscribe(listener: (value: NavigationSnapshot) => void) {
    this.listeners.add(listener)
    if (this.listeners.size === 1) void this.refresh()
    return () => {
      this.listeners.delete(listener)
      if (this.listeners.size === 0) { this.generation += 1; this.previewSequence += 1; clearTimeout(this.refreshTimer); this.refreshRequest?.abort(); this.confirmation.clear() }
    }
  }
  private publish(value: NavigationSnapshot) {
    this.snapshot = Object.freeze({ ...value, reviewSupported: true })
    for (const listener of this.listeners) listener(this.snapshot)
  }
  private async refresh() {
    const generation = ++this.generation
    const started = this.now()
    const controller = new AbortController()
    this.refreshRequest = controller
    try {
      const value = await this.http.request('/navigation/catalog', undefined, controller.signal)
      if (generation !== this.generation) return
      if (!isRecord(value) || value.status !== 'ready') throw new Error('Accepted destinations are unavailable.')
      const catalog = parseNavigationCatalog(rebase(value.catalog, value.serverNowMs, started, this.now()))
      if (!catalog || catalog.session !== this.http.connection.sessionId) throw new Error('The relay returned an invalid destination catalog.')
      const prior = this.snapshot.catalog
      const identity = (c: NavigationCatalog) => JSON.stringify({ ...c, receivedAt: 0, expiresAt: 0 })
      if (prior && identity(prior) === identity(catalog) && prior.expiresAt > this.now()) {
        if (this.snapshot.status !== 'ready') this.publish({ ...this.snapshot, status: 'ready', reason: null })
      } else {
        this.previewSequence += 1
        this.confirmation.clear()
        this.publish({ status: 'ready', reason: null, catalog, preview: null })
      }
    } catch (error) {
      if (generation === this.generation) {
        this.previewSequence += 1
        this.confirmation.clear()
        this.publish({ status: 'unavailable', reason: error instanceof Error ? error.message : 'Navigation is unavailable.', catalog: null, preview: null })
      }
    } finally {
      if (generation === this.generation && this.listeners.size) this.refreshTimer = setTimeout(() => void this.refresh(), 2000)
    }
  }
  async requestPreview(request: NavigationPreviewRequest): Promise<NavigationPreview> {
    const sequence = ++this.previewSequence
    const catalog = this.snapshot.catalog
    const expected: NavigationPreviewRequest = JSON.parse(JSON.stringify(request))
    if (!catalog || expected.session !== this.http.connection.sessionId || expected.catalogVersion !== catalog.catalogVersion
      || expected.configVersion !== catalog.configVersion || !equalNavigationEvidence(expected.map, catalog.map)
      || !equalNavigationEvidence(expected.motionConfig, catalog.motionConfig)) throw new Error('The requested navigation catalog is no longer current.')
    const started = this.now()
    const result = await this.http.request('/navigation/preview', expected)
    if (sequence !== this.previewSequence || catalog !== this.snapshot.catalog || !catalog) throw new Error('The accepted destination catalog or review changed during review.')
    if (!isRecord(result) || typeof result.previewHash !== 'string' || !/^[a-f0-9]{64}$/.test(result.previewHash)) throw new Error('The relay returned invalid preview evidence.')
    const preview = parseNavigationPreview(rebase(result.preview, result.serverNowMs, started, this.now()))
    const validity = navigationPreviewValidity(preview, catalog, { session: expected.session, rosterVersion: expected.rosterVersion,
      selected: expected.selected, destinationZoneId: expected.zoneId, intentId: expected.intentId, now: this.now(), reviewOnly: true })
    if (!preview || (!validity.valid && validity.code !== 'node_refused')) throw new Error('The relay returned a preview for another request.')
    this.confirmation.clear()
    this.confirmation.set(preview.previewId, { preview, hash: result.previewHash })
    this.publish({ ...this.snapshot, preview })
    return preview
  }
  async confirmPreview(preview: NavigationPreview): Promise<unknown> {
    const binding = this.confirmation.get(preview.previewId)
    if (!binding || !equalNavigationEvidence(binding.preview, preview) || preview.expiresAt <= this.now()) throw new Error('The frozen navigation review is no longer current.')
    this.confirmation.delete(preview.previewId)
    const result = await this.http.request('/navigation/confirm', { previewId: preview.previewId, intentId: preview.intentId, previewHash: binding.hash })
    if (!isRecord(result) || result.previewId !== preview.previewId || result.intentId !== preview.intentId
      || !['accepted', 'refused', 'invalidated'].includes(String(result.status)) || typeof result.dispatchEligible !== 'boolean'
      || (result.status === 'accepted') !== (result.dispatchEligible === true)
      || typeof result.code !== 'string' || result.code.length > 128 || typeof result.detail !== 'string' || result.detail.length > 2048) throw new Error('The relay returned an invalid confirmation outcome.')
    return result
  }
}
