import type { MemoryAnalysisRequest } from './types'

const PREFIX = 'sweep.atlas.analysis.v1.'
export const RECOVERY_CHANGED = 'sweep-memory-analysis-recovery'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const BROKEN = 'Saved analysis recovery could not be read. It has not been overwritten.'
type Request = MemoryAnalysisRequest & { request_id: string }
type Entries = Record<string, Request>

function parseRequest(value: unknown): Request {
  const v = value as Request | null
  if (!v || typeof v !== 'object' || Object.keys(v).sort().join(',') !== 'ai,audio_asset_id,request_id,revision,weather' ||
    !Number.isSafeInteger(v.revision) || v.revision < 0 ||
    typeof v.ai !== 'boolean' || typeof v.weather !== 'boolean' ||
    typeof v.request_id !== 'string' || !UUID.test(v.request_id) ||
    !(v.audio_asset_id === null || typeof v.audio_asset_id === 'string' && UUID.test(v.audio_asset_id)))
    throw new Error(BROKEN)
  return { revision: v.revision, ai: v.ai, weather: v.weather, audio_asset_id: v.audio_asset_id, request_id: v.request_id }
}
async function digest(parts: readonly string[]) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(parts)))
  return Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, '0')).join('')
}

/** Bounded pending intent, not a media cache. Exact credential/Space/capture scopes are hashed. */
export class AnalysisRecoveryStore {
  private key: Promise<string> | undefined
  private readonly identity: readonly string[]
  constructor(identity: readonly string[]) { this.identity = Object.freeze([...identity]) }
  private async keys(space: string, capture: string) {
    this.key ??= digest(this.identity).then(hash => PREFIX + hash)
    return [await this.key, await digest([space, capture])] as const
  }
  private entries(key: string): Entries {
    const raw = localStorage.getItem(key)
    if (!raw) return {}
    if (raw.length > 16_384) throw new Error(BROKEN)
    let data: unknown
    try { data = JSON.parse(raw) } catch { throw new Error(BROKEN) }
    if (!data || typeof data !== 'object' || Array.isArray(data) || Object.keys(data).length > 20)
      throw new Error(BROKEN)
    return Object.fromEntries(Object.entries(data).map(([id, request]) => {
      if (!/^[0-9a-f]{64}$/.test(id)) throw new Error(BROKEN)
      return [id, parseRequest(request)]
    }))
  }
  private write(key: string, entries: Entries) {
    if (Object.keys(entries).length) localStorage.setItem(key, JSON.stringify(entries))
    else localStorage.removeItem(key)
    window.dispatchEvent(new Event(RECOVERY_CHANGED))
  }
  private async locked<T>(key: string, action: () => T): Promise<T> {
    if (!navigator.locks) throw new Error('Analysis recovery needs an updated browser over HTTPS or localhost. No analysis was sent.')
    return navigator.locks.request(key, action)
  }
  async read(space: string, capture: string): Promise<Request | null> {
    const [key, id] = await this.keys(space, capture)
    return this.entries(key)[id] ?? null
  }
  async reserve(space: string, capture: string, proposed: MemoryAnalysisRequest) {
    const request = parseRequest(proposed)
    const [key, id] = await this.keys(space, capture)
    return this.locked(key, () => {
      const entries = this.entries(key)
      if (entries[id]) return { request: entries[id], existing: true }
      if (Object.keys(entries).length >= 20)
        throw new Error('Recover an unfinished analysis before starting another. This workspace has 20 recovery references on this browser.')
      entries[id] = request
      this.write(key, entries)
      return { request, existing: false }
    })
  }
  async acknowledge(space: string, capture: string, requestId: string): Promise<Request | null> {
    const [key, id] = await this.keys(space, capture)
    return this.locked(key, () => {
      const entries = this.entries(key)
      if (entries[id]?.request_id === requestId) {
        delete entries[id]
        this.write(key, entries)
      }
      return entries[id] ?? null // A late acknowledgement must not clear a newer intent.
    })
  }
  async forgetRemoved(space: string, capture: string) {
    const [key, id] = await this.keys(space, capture)
    return this.locked(key, () => {
      const entries = this.entries(key)
      if (entries[id]) { delete entries[id]; this.write(key, entries) }
    })
  }
}
