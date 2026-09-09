import { AtlasClient } from './client'
import type { PlatformConnection, PlatformFetch } from '../platform/http'
import { relayHttpUrl } from '../relay/origin'
import type { CaptureResponseTarget, SpaceDetail } from './types'
import { parseDraft, type SpaceDraftStore } from './drafts'

export interface NativeSession extends Omit<PlatformConnection, 'token'> { id: string; space: string | null }
export interface NativeUpload {
  id: string; spaceId: string; kind: 'photo' | 'video' | 'panorama'
  state: 'capturing' | 'importing' | 'queued' | 'uploading' | 'saved' | 'failed'
  bytes: number; sent: number; error: string; createdAt: number
  source: 'camera' | 'import'; finalized: boolean
  displayName?: string
  responseTo?: CaptureResponseTarget
}
interface MessagePort {
  postMessage: (message: string) => void
  onmessage: ((event: { data: string }) => void) | null
}
declare global { interface Window { SweepAtlasNative?: MessagePort } }
class NativeError extends Error { code = '' }
const pending = new Map<string, { resolve: (value: unknown) => void; reject: (reason: Error) => void; timer: ReturnType<typeof setTimeout> }>()
let listening: MessagePort | undefined

/** Only the installed Android origin exposes this message port. No credential is returned by it. */
export function nativeCall<T>(op: string, payload: unknown = {}): Promise<T> {
  const port = window.SweepAtlasNative
  if (!port) return Promise.reject(new Error('Open Atlas in the Android app to use native capture.'))
  if (listening !== port) {
    listening = port
    port.onmessage = event => {
      let message: { id: string; result?: unknown; error?: string; code?: string }
      try { message = JSON.parse(event.data) } catch { return }
      const request = pending.get(message.id)
      if (!request) return
      clearTimeout(request.timer); pending.delete(message.id)
      if (message.error) {
        const error = new NativeError(message.error); error.code = message.code ?? ''
        request.reject(error)
      } else request.resolve(message.result)
    }
  }
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID()
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Android did not finish this action. Please try again.')) }, 65_000)
    pending.set(id, { resolve: value => resolve(value as T), reject, timer })
    try { port.postMessage(JSON.stringify({ id, op, payload })) }
    catch (error) { clearTimeout(timer); pending.delete(id); reject(error) }
  })
}

export function nativeFetch(session: NativeSession): PlatformFetch {
  const prefix = relayHttpUrl(session.baseUrl, `/api/sessions/${encodeURIComponent(session.sessionId)}`)!
  return async (input, init) => {
    if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const url = String(input)
    if (!url.startsWith(`${prefix}/atlas/spaces`)) throw new Error('This request is outside Atlas.')
    const path = url.slice(prefix.length)
    // Media streams directly through the bounded native HTTP interceptor, not a base64 message.
    if (path.endsWith('/media') || path.endsWith('/cloud.glb')) {
      return globalThis.fetch(`https://appassets.androidplatform.net/atlas-data/${session.id}${path}`, {
        signal: init?.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
      })
    }
    const result = await nativeCall<{ status: number; body: string }>('request', {
      session: session.id, path, method: init?.method ?? 'GET', body: init?.body ?? '',
    })
    if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    return new Response(result.body, { status: result.status, headers: { 'Content-Type': 'application/json' } })
  }
}

/** Cached metadata is explicitly stale; live people are stripped natively before persistence. */
export class NativeAtlasClient extends AtlasClient {
  readonly session: NativeSession
  private readonly network: (offline: boolean) => void
  private readonly nativeDrafts: SpaceDraftStore
  override get drafts(): SpaceDraftStore { return this.nativeDrafts }
  constructor(session: NativeSession, network: (offline: boolean) => void = () => {}) {
    super({ ...session, token: '' }, nativeFetch(session))
    this.session = session; this.network = network
    this.nativeDrafts = {
      read: async () => { const value = await nativeCall<unknown>('readDraft', { session: session.id }); return value === null ? null : parseDraft(value) },
      write: (draft, previous) => nativeCall('writeDraft', { session: session.id, draft, previous }),
      remove: previous => nativeCall('removeDraft', { session: session.id, previous }),
    }
  }
  override async list(signal?: AbortSignal) {
    try { const spaces = await super.list(signal); this.network(false); return spaces }
    catch (error) {
      if (!(error instanceof NativeError) || error.code !== 'network') throw error
      const cached = await nativeCall<SpaceDetail[]>('cachedSpaces', { session: this.session.id })
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      this.network(true)
      return cached.map(detail => detail.space)
    }
  }
  override async detail(id: string, signal?: AbortSignal): Promise<SpaceDetail> {
    try {
      const detail = await super.detail(id, signal)
      this.network(false)
      await nativeCall('cacheSpace', { session: this.session.id, detail }).catch(() => {})
      return detail
    } catch (error) {
      if (!(error instanceof NativeError) || error.code !== 'network') throw error
      const cached = await nativeCall<SpaceDetail[]>('cachedSpaces', { session: this.session.id })
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const detail = cached.find(value => value.space.id === id)
      if (!detail) throw new Error('This space has not been saved on this device. Reconnect to open it.', { cause: error })
      this.network(true)
      return { ...detail, people: [] }
    }
  }
}
