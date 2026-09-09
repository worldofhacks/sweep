import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AccountSpaces from './AccountSpaces'
import { publishAccountSession, type AccountSession } from './accountSession'
import { clearPendingInvitation } from './accountClient'

vi.mock('../atlas/SpacesModule', () => ({ WorkspaceSpaces: ({ accountRole, onExitAccount }: {
  accountRole: string; onExitAccount: () => void
}) => <div><p>Existing Space view · {accountRole}</p><button onClick={onExitAccount}>Shared with me</button></div> }))
const session: AccountSession = { key: 'user:session', userId: 'user', getToken: async () => 'test-signed-token' }
const space = { space: { id: 'garden', title: 'Austin garden', place: 'Austin, Texas', description: 'Neighbors growing together.' }, session: 'austin', role: 'viewer' }
beforeEach(() => { vi.stubEnv('VITE_ATLAS_API_ORIGIN', 'https://atlas.example'); publishAccountSession(session) })
afterEach(() => { publishAccountSession(null); clearPendingInvitation(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
function api() {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const value = url.endsWith('/preview') ? { title: 'Austin garden', place: 'Austin, Texas', role: 'viewer', expires_at: Date.now() + 3600000 }
      : url.endsWith('/accept') ? { space_id: 'garden', session: 'austin', role: 'viewer' }
      : { spaces: [space] }
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
function view(props: Partial<Parameters<typeof AccountSpaces>[0]> = {}) {
  return render(<AccountSpaces session={session} pending="" onHandled={vi.fn()} onClose={vi.fn()} {...props} />)
}

it('requires an explicit reviewed join and then opens the existing viewer surface', async () => {
  const fetcher = api()
  const handled = vi.fn()
  view({ pending: 'a'.repeat(43), onHandled: handled })
  await screen.findByText('Austin garden')
  expect(fetcher.mock.calls.some(call => String(call[0]).endsWith('/accept'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Review invitation' }))
  expect(await screen.findByLabelText('Invitation permissions')).toHaveTextContent('cannot add or change anything')
  expect(fetcher.mock.calls.some(call => String(call[0]).endsWith('/accept'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Join this space' }))
  expect(await screen.findByText('Existing Space view · viewer')).toBeInTheDocument()
  expect(handled).toHaveBeenCalledOnce()
})

it('does not contact an account API when signed out and discards private views on sign-out', async () => {
  const fetcher = api()
  const rendered = view({ session: null })
  expect(fetcher).not.toHaveBeenCalled()
  expect(screen.getByText(/Sign in using the account button/)).toBeInTheDocument()
  rendered.rerender(<AccountSpaces session={session} pending="" onHandled={vi.fn()} onClose={vi.fn()} />)
  await screen.findByText('Austin garden')
  act(() => publishAccountSession(null))
  rendered.rerender(<AccountSpaces session={null} pending="" onHandled={vi.fn()} onClose={vi.fn()} />)
  expect(screen.queryByText('Austin garden')).not.toBeInTheDocument()
})

it('confirms before leaving and reports backend setup failures', async () => {
  const fetcher = api()
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Leave space' }))
  expect(fetcher.mock.calls.some(call => String(call[0]).endsWith('/membership'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm leaving' }))
  await waitFor(() => expect(fetcher.mock.calls.some(call => String(call[0]).endsWith('/membership'))).toBe(true))
  expect(await screen.findByText('You’ve left the space.')).toBeInTheDocument()
})

it('shows unavailable server configuration rather than a fabricated account directory', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Account sign-in is not configured for this workspace.' }), { status: 503 })))
  view()
  expect(await screen.findByRole('alert')).toHaveTextContent('not configured')
  expect(screen.queryByRole('button', { name: /Open space/ })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create a space' })).toBeDisabled()
})

it('opens owned spaces without offering to orphan them by leaving', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ spaces: [{ ...space, role: 'owner' }] }))))
  view()
  expect(await screen.findByText('YOUR SPACE')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Leave space' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Open space/ }))
  expect(await screen.findByText('Existing Space view · owner')).toBeInTheDocument()
})
