import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AccountClient, clearPendingInvitation, invitationLink, invitationToken, pendingInvitation } from './accountClient'
import { publishAccountSession, type AccountSession } from './accountSession'

let session: AccountSession
beforeEach(() => {
  vi.stubEnv('VITE_ATLAS_API_ORIGIN', 'https://atlas.example')
  session = { key: 'user:session', userId: 'user', getToken: vi.fn().mockResolvedValue('signed-test-session') }
  publishAccountSession(session)
  clearPendingInvitation()
  window.history.replaceState(null, '', '/')
})
afterEach(() => { publishAccountSession(null); clearPendingInvitation(); vi.unstubAllEnvs(); vi.restoreAllMocks() })

it('refreshes tokens for account and capture requests without modifying operator connections', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ spaces: [] })))
  const api = new AccountClient(session, fetcher)
  await api.list()
  expect(new Headers(fetcher.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer signed-test-session')
  fetcher.mockResolvedValue(new Response(JSON.stringify({ captures: [] })))
  const client = api.space({ space_id: 'garden', session: 'austin', role: 'viewer' })
  await client.detail('garden')
  expect(session.getToken).toHaveBeenCalledTimes(2)
  expect(client.connection.token).toBe('account:user')
  expect(fetcher.mock.calls[1][0]).toBe('https://atlas.example/api/sessions/austin/atlas/spaces/garden')
  expect(fetcher.mock.calls[1][1]).toMatchObject({ credentials: 'omit', redirect: 'error', cache: 'no-store' })
})

it('never sends an account token to arbitrary invitation/operator origins or fleet endpoints', async () => {
  const fetcher = vi.fn()
  const api = new AccountClient(session, fetcher)
  for (const url of ['https://other.example/api/atlas/account', 'https://atlas.example/metrics',
    'https://atlas.example/api/atlas/account-elsewhere', 'https://atlas.example/api/atlas/account?redirect=other'])
    await expect(api.fetcher(url)).rejects.toThrow('cannot be sent')
  expect(fetcher).not.toHaveBeenCalled()
  expect(session.getToken).not.toHaveBeenCalled()
})

it('discards an in-flight token when the account changes', async () => {
  let finish!: (value: string) => void
  session.getToken = vi.fn(() => new Promise<string>(resolve => { finish = resolve }))
  const fetcher = vi.fn()
  const promise = new AccountClient(session, fetcher).list()
  publishAccountSession(null)
  finish('late-token')
  await expect(promise).rejects.toThrow('account changed')
  expect(fetcher).not.toHaveBeenCalled()
})

it('aborts waiting for the SDK rather than leaving a request stuck', async () => {
  session.getToken = vi.fn(() => new Promise<string | null>(() => {}))
  const controller = new AbortController()
  const fetcher = vi.fn()
  const promise = new AccountClient(session, fetcher).list(controller.signal)
  controller.abort()
  await expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetcher).not.toHaveBeenCalled()
})

it('retains only a bounded one-time invitation across sign-in redirects and strips the fragment', () => {
  const token = 'a'.repeat(43)
  window.history.replaceState(null, '', '/#account-invite=' + token)
  expect(pendingInvitation()).toBe(token)
  expect(window.location.hash).toBe('')
  expect(pendingInvitation()).toBe(token)
  expect(invitationToken(invitationLink(token))).toBe(token)
  expect(() => invitationToken('https://other.example/#account-invite=' + token)).toThrow()
  expect(() => invitationToken('arbitrary session token')).toThrow()
  clearPendingInvitation()
  expect(pendingInvitation()).toBe('')
})

it('ignores malformed invitation fragments and expired saved invitations', () => {
  window.history.replaceState(null, '', '/#account-invite=bad&relay=https://other.example')
  expect(pendingInvitation()).toBe('')
  sessionStorage.setItem('sweep.atlas.pending-account-invitation', JSON.stringify({ token: 'a'.repeat(43), savedAt: Date.now() - 25 * 3600_000 }))
  expect(pendingInvitation()).toBe('')
})

it('sends preview and acceptance separately, without placing the invitation in a URL', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ title: 'Garden', place: 'Austin', expires_at: Date.now() + 3600000, space_id: 'garden', session: 'austin', role: 'viewer' }))))
  const api = new AccountClient(session, fetcher)
  await api.preview('a'.repeat(43))
  await api.accept('a'.repeat(43))
  expect(fetcher.mock.calls.map(call => call[0])).toEqual([
    'https://atlas.example/api/atlas/account/invitations/preview',
    'https://atlas.example/api/atlas/account/invitations/accept',
  ])
  expect(fetcher.mock.calls[0][1].body).toBe(JSON.stringify({ token: 'a'.repeat(43) }))
})

it('creates account-owned spaces with a matching draft receipt and accepts owner directory entries', async () => {
  const draft = '42e4ab24-b2eb-4a80-bf04-2663781221c1'
  const space = { title: 'Our Austin garden', place: 'Austin, Texas', latitude: 30.27, longitude: -97.74, description: '', category: 'community' as const, radius: 80 }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ draft_id: draft, space_id: 'garden', session: 'account-only', role: 'owner' })))
  const api = new AccountClient(session, fetcher)
  expect(await api.create(draft, space)).toMatchObject({ role: 'owner' })
  expect(fetcher.mock.calls[0][0]).toBe('https://atlas.example/api/atlas/account/spaces')
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ draft_id: draft, space })
  fetcher.mockResolvedValue(new Response(JSON.stringify({ spaces: [{ space: { ...space, id: 'garden' }, session: 'account-only', role: 'owner' }] })))
  expect((await api.list())[0].role).toBe('owner')
  fetcher.mockResolvedValue(new Response(JSON.stringify({ draft_id: 'different', space_id: 'garden', session: 'account-only', role: 'owner' })))
  await expect(api.create(draft, space)).rejects.toThrow('not confirmed')
})
