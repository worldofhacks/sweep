import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SpaceSharing from './SpaceSharing'
import { AtlasClient } from '../atlas/client'

beforeEach(() => vi.stubEnv('VITE_ATLAS_API_ORIGIN', 'https://atlas.example'))
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs() })
function setup(enabled = true) {
  const client = new AtlasClient({ baseUrl: 'https://atlas.example', sessionId: 'austin', token: 'test-operator' })
  vi.spyOn(client, 'accountInvitations').mockResolvedValue({ enabled, invitations: [] })
  vi.spyOn(client, 'members').mockResolvedValue({ members: [] })
  const invite = vi.spyOn(client, 'inviteAccount').mockResolvedValue({ id: 'invitation-id', token: 'a'.repeat(43), role: 'viewer', expires_at: Date.now() + 86400_000 })
  return { client, invite, legacyInvitation: vi.fn().mockResolvedValue('legacy test invitation'), replaceLegacy: vi.fn().mockResolvedValue('replacement') }
}

it('does not create any grant on opening and makes the chosen role explicit', async () => {
  const value = setup()
  render(<SpaceSharing {...value} spaceId="garden" />)
  fireEvent.click(await screen.findByRole('button', { name: /Just explore/ }))
  expect(value.invite).not.toHaveBeenCalled()
  expect(value.legacyInvitation).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Create account invitation' }))
  expect(await screen.findByLabelText('Account invitation link')).toHaveValue(window.location.origin + '/#account-invite=' + 'a'.repeat(43))
  expect(value.invite).toHaveBeenCalledWith('garden', 'viewer', 24)
  expect(screen.getByText(/current and future captures/)).toBeInTheDocument()
})

it('keeps account setup honest while retaining the legacy invitation action', async () => {
  const value = setup(false)
  render(<SpaceSharing {...value} spaceId="garden" />)
  expect(await screen.findByText(/Account invitations need sign-in configured/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Create account invitation' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('Existing contribution links'))
  fireEvent.click(screen.getByRole('button', { name: 'Show contribution invitation' }))
  expect(await screen.findByLabelText('Space invitation')).toHaveValue('legacy test invitation')
  expect(value.legacyInvitation).toHaveBeenCalledOnce()
})

it('requires confirmation before removing membership', async () => {
  const value = setup()
  vi.mocked(value.client.members).mockResolvedValue({ members: [{ account_id: 'acct_alex', role: 'viewer', joined_at: 1 }] })
  const remove = vi.spyOn(value.client, 'removeMember').mockResolvedValue({ removed: true })
  render(<SpaceSharing {...value} spaceId="garden" />)
  fireEvent.click(await screen.findByRole('button', { name: 'Remove access' }))
  expect(remove).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith('garden', 'acct_alex'))
  expect(await screen.findByText('Account membership removed.')).toBeInTheDocument()
})

it('does not call web account routes from the native sharing surface', () => {
  const value = setup()
  render(<SpaceSharing {...value} spaceId="garden" allowAccounts={false} />)
  expect(value.client.accountInvitations).not.toHaveBeenCalled()
  expect(value.client.members).not.toHaveBeenCalled()
  expect(screen.getByText(/managed in the web console/)).toBeInTheDocument()
})

it('protects owner membership and does not offer anonymous grants in account-owned spaces', async () => {
  const value = setup()
  vi.mocked(value.client.members).mockResolvedValue({ members: [{ account_id: 'acct_owner', role: 'owner', joined_at: 1 }] })
  render(<SpaceSharing {...value} spaceId="garden" allowLegacy={false} />)
  expect(await screen.findByText('Owner')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Remove access' })).not.toBeInTheDocument()
  expect(screen.queryByText('Existing contribution links')).not.toBeInTheDocument()
  expect(value.legacyInvitation).not.toHaveBeenCalled()
})
