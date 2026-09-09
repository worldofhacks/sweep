import type { PlatformConnection } from '../platform/http'
import type { NewSpace } from './types'

export const EMPTY_SPACE: NewSpace = { title: '', description: '', category: 'community',
  latitude: 0, longitude: 0, radius: 80, place: '' }
export interface SpaceDraft {
  version: 1
  id: string
  revision: number
  updatedAt: number
  space: NewSpace
  coordinates: [string, string]
  /** Frozen before the first HTTP attempt. A lost reply must retry these exact fields. */
  submitted: NewSpace | null
}
export interface SpaceDraftStore {
  read(): Promise<SpaceDraft | null>
  write(value: SpaceDraft, previous: SpaceDraft | null): Promise<void>
  remove(previous: SpaceDraft): Promise<void>
}
const CONFLICT = 'This draft changed in another window. Reopen Spaces to load the saved version.'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
function validSpace(value: unknown): value is NewSpace {
  if (!value || typeof value !== 'object') return false
  const v = value as NewSpace
  return typeof v.title === 'string' && v.title.length <= 100 &&
    typeof v.description === 'string' && v.description.length <= 2000 &&
    typeof v.place === 'string' && v.place.length <= 120 &&
    ['incident', 'hazard', 'community', 'survey'].includes(v.category) &&
    Number.isFinite(v.latitude) && Number.isFinite(v.longitude) &&
    Number.isInteger(v.radius) && v.radius >= 20 && v.radius <= 500
}
export function parseDraft(value: unknown): SpaceDraft {
  const v = value as SpaceDraft | null
  if (!v || v.version !== 1 || !UUID.test(v.id) || !Number.isSafeInteger(v.revision) || v.revision < 1 ||
    !Number.isSafeInteger(v.updatedAt) || v.updatedAt <= 0 || !validSpace(v.space) ||
    !Array.isArray(v.coordinates) || v.coordinates.length !== 2 ||
    !v.coordinates.every(item => typeof item === 'string' && item.length <= 64) ||
    (v.submitted !== null && !validSpace(v.submitted))) throw new Error('The saved draft could not be read. It has not been overwritten.')
  return v
}
export function assertDraftRevision(current: SpaceDraft | null, previous: SpaceDraft | null) {
  if (current?.id !== previous?.id || current?.revision !== previous?.revision) throw new Error(CONFLICT)
}

/** One private draft per exact workspace credential, never a bearer token in storage. */
export class BrowserSpaceDraftStore implements SpaceDraftStore {
  private key: Promise<string> | undefined
  private readonly connection: PlatformConnection
  constructor(connection: PlatformConnection) { this.connection = connection }
  private storageKey() {
    return this.key ??= (async () => {
      const c = this.connection
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(
        JSON.stringify([c.baseUrl, c.sessionId, c.token])))
      return 'sweep.atlas.draft.v1.' + Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('')
    })()
  }
  private readKey(key: string) {
    const raw = localStorage.getItem(key)
    if (raw && raw.length > 32_768) throw new Error('The saved draft is too large. It has not been overwritten.')
    return raw ? parseDraft(JSON.parse(raw)) : null
  }
  async read() { return this.readKey(await this.storageKey()) }
  async write(value: SpaceDraft, previous: SpaceDraft | null) {
    const key = await this.storageKey()
    if (!navigator.locks) throw new Error('Private drafts need an updated browser over HTTPS or localhost.')
    await navigator.locks.request(key, () => {
      assertDraftRevision(this.readKey(key), previous)
      localStorage.setItem(key, JSON.stringify(parseDraft(value)))
    })
  }
  async remove(previous: SpaceDraft) {
    const key = await this.storageKey()
    if (!navigator.locks) throw new Error('Private drafts need an updated browser over HTTPS or localhost.')
    await navigator.locks.request(key, () => {
      assertDraftRevision(this.readKey(key), previous)
      localStorage.removeItem(key)
    })
  }
}
