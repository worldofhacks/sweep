import { AtlasClient } from '../atlas/client'
import type { NewSpace, Space } from '../atlas/types'
import type { PlatformFetch } from '../platform/http'
import { readPlatformResponse } from '../platform/http'
import { currentAccountSession, type AccountSession } from './accountSession'

export type AccountRole = 'viewer' | 'contributor' | 'owner'
export interface JoinedSpace { space: Space; session: string; role: AccountRole }
export interface AccountGrant { space_id: string; session: string; role: AccountRole }
export interface InvitePreview { title: string; place: string; role: Exclude<AccountRole, 'owner'>; expires_at: number }
const tokenPattern = /^[a-zA-Z0-9_-]{43}$/
const pendingKey = 'sweep.atlas.pending-account-invitation'
let transientInvitation: { token: string; expires: number } | null = null

export function accountApiOrigin(): string {
  const url = new URL(import.meta.env.VITE_ATLAS_API_ORIGIN || window.location.origin)
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))) ||
      url.pathname !== '/' || url.username || url.password || url.search || url.hash)
    throw new Error('Account sharing needs a configured HTTPS API origin.')
  return url.origin
}

export function invitationToken(value: string): string {
  const raw = value.trim()
  if (tokenPattern.test(raw)) return raw
  let url: URL
  try { url = new URL(raw) } catch { throw new Error('Paste an account invitation link or code, not a workspace connection.') }
  const parts = new URLSearchParams(url.hash.slice(1))
  const token = parts.get('account-invite') ?? ''
  if (url.origin !== window.location.origin || url.username || url.password || parts.size !== 1 || !tokenPattern.test(token))
    throw new Error('Use an account invitation for this Sweep application.')
  return token
}

export function invitationLink(token: string) {
  if (!tokenPattern.test(token)) throw new Error('The invitation was not returned correctly.')
  return `${window.location.origin}${window.location.pathname}#account-invite=${token}`
}

/** Preserve only a one-time invitation in this tab across OAuth redirects, never a session token. */
export function pendingInvitation(): string {
  const hash = new URLSearchParams(window.location.hash.slice(1))
  if (hash.has('account-invite')) {
    let token = ''
    try { token = invitationToken(window.location.href) } catch { /* malformed links grant nothing */ }
    window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search)
    if (token) {
      transientInvitation = { token, expires: Date.now() + 24 * 3600_000 }
      try { sessionStorage.setItem(pendingKey, JSON.stringify({ token, savedAt: Date.now() })) } catch { /* manual paste remains available */ }
      return token
    }
  }
  if (transientInvitation && transientInvitation.expires > Date.now()) return transientInvitation.token
  transientInvitation = null
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingKey) ?? 'null')
    if (saved && typeof saved.token === 'string' && tokenPattern.test(saved.token) && typeof saved.savedAt === 'number' &&
      Date.now() >= saved.savedAt && Date.now() - saved.savedAt < 24 * 3600_000) return saved.token
    sessionStorage.removeItem(pendingKey)
  } catch { /* storage is optional */ }
  return ''
}
export function clearPendingInvitation() {
  transientInvitation = null
  try { sessionStorage.removeItem(pendingKey) } catch { /* storage is optional */ }
}

export class AccountClient {
  readonly origin: string
  readonly fetcher: PlatformFetch
  readonly session: AccountSession
  constructor(session: AccountSession, fetcher: PlatformFetch = (url, init) => fetch(url, init)) {
    this.session = session
    this.origin = accountApiOrigin()
    this.fetcher = async (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
      if (url.origin !== this.origin || url.username || url.password || url.search || url.hash ||
        !(/^\/api\/atlas\/account(?:\/|$)/.test(url.pathname) || /^\/api\/sessions\/[^/]+\/atlas\/spaces(?:\/|$)/.test(url.pathname)))
        throw new Error('Account credentials cannot be sent to that endpoint.')
      this.assertCurrent()
      const token = await sessionToken(session, init?.signal)
      this.assertCurrent()
      if (!token) throw new Error('Sign in again to open your shared spaces.')
      if (init?.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError')
      const headers = new Headers(init?.headers)
      headers.set('Authorization', `Bearer ${token}`)
      const response = await fetcher(url.href, { ...init, headers, credentials: 'omit', redirect: 'error', cache: 'no-store' })
      this.assertCurrent()
      return response
    }
  }
  private assertCurrent() {
    if (currentAccountSession()?.key !== this.session.key)
      throw new Error('Your account changed. Reopen your shared spaces.')
  }
  private async request(path: string, body?: unknown, signal?: AbortSignal, method?: string) {
    const response = await this.fetcher(this.origin + '/api/atlas/account' + path, {
      method: method ?? (body === undefined ? 'GET' : 'POST'),
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(10_000)]) : AbortSignal.timeout(10_000),
    })
    const value = await readPlatformResponse(response)
    this.assertCurrent()
    return value
  }
  async list(signal?: AbortSignal): Promise<JoinedSpace[]> {
    const value = await this.request('/spaces', undefined, signal) as { spaces: JoinedSpace[] }
    if (!Array.isArray(value.spaces) || value.spaces.some(item =>
      !item.space?.id || !item.session || !['viewer', 'contributor', 'owner'].includes(item.role)))
      throw new Error('The shared space directory is unavailable.')
    return value.spaces
  }
  async create(draft_id: string, space: NewSpace): Promise<AccountGrant> {
    const value = await this.request('/spaces', { draft_id, space }) as AccountGrant & { draft_id: string }
    if (value.draft_id !== draft_id || !value.space_id || !value.session || value.role !== 'owner')
      throw new Error('Creation was not confirmed. Retry this same draft or check your spaces.')
    return value
  }
  async preview(token: string): Promise<InvitePreview> {
    const value = await this.request('/invitations/preview', { token }) as InvitePreview
    if (typeof value.title !== 'string' || typeof value.place !== 'string' || !Number.isFinite(value.expires_at) ||
      !['viewer', 'contributor'].includes(value.role)) throw new Error('The invitation permissions could not be verified.')
    return value
  }
  async accept(token: string): Promise<AccountGrant> {
    const value = await this.request('/invitations/accept', { token }) as AccountGrant
    if (!value.space_id || !value.session || !['viewer', 'contributor'].includes(value.role))
      throw new Error('Membership was not confirmed. Refresh your shared spaces.')
    return value
  }
  async leave(id: string) { return this.request(`/spaces/${encodeURIComponent(id)}/membership`, undefined, undefined, 'DELETE') }
  space(grant: AccountGrant) {
    return new AtlasClient({ baseUrl: this.origin, sessionId: grant.session,
      token: `account:${this.session.userId}` }, this.fetcher)
  }
}

function sessionToken(session: AccountSession, signal?: AbortSignal | null): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const abort = () => { signal?.removeEventListener('abort', abort); reject(new DOMException('Request cancelled', 'AbortError')) }
    if (signal?.aborted) { abort(); return }
    signal?.addEventListener('abort', abort, { once: true })
    void session.getToken().then(value => {
      signal?.removeEventListener('abort', abort); resolve(value)
    }, error => { signal?.removeEventListener('abort', abort); reject(error) })
  })
}
