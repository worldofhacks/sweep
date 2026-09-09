import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import ClerkAccount from './ClerkAccount'

const state = vi.hoisted(() => ({ loaded: true, signedIn: false, firstName: '', provider: vi.fn(), signIn: vi.fn() }))
vi.mock('@clerk/react', () => ({
  ClerkProvider: (props: { children: ReactNode }) => { state.provider(props); return props.children },
  SignInButton: (props: { children: ReactNode }) => { state.signIn(props); return props.children },
  UserButton: () => <button>Manage your account</button>,
  useUser: () => ({ isLoaded: state.loaded, isSignedIn: state.signedIn, user: { firstName: state.firstName } }),
}))
beforeEach(() => { state.loaded = true; state.signedIn = false; state.firstName = ''; vi.clearAllMocks() })

it('hands sign-in to Clerk without forwarding workspace invitation tokens in the return URL', () => {
  window.history.replaceState(null, '', '/?debug=local#workspace-invitation')
  render(<ClerkAccount publishableKey="pk_test_example" />)
  expect(screen.getByRole('button', { name: 'Join in / Sign in' })).toBeInTheDocument()
  expect(state.provider).toHaveBeenCalledWith(expect.objectContaining({ publishableKey: 'pk_test_example' }))
  expect(state.signIn).toHaveBeenCalledWith(expect.objectContaining({ mode: 'modal', forceRedirectUrl: window.location.origin + '/' }))
  window.history.replaceState(null, '', '/')
})

it('shows Clerk account management after sign-in without changing workspace access', () => {
  state.signedIn = true; state.firstName = 'Taylor'
  render(<ClerkAccount publishableKey="pk_test_example" />)
  expect(screen.getByText('Taylor')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Manage your account' })).toBeInTheDocument()
  expect(state.signIn).not.toHaveBeenCalled()
})

it('explains session loading before offering sign-in', () => {
  state.loaded = false
  render(<ClerkAccount publishableKey="pk_test_example" />)
  expect(screen.getByRole('status')).toHaveTextContent('Opening sign-in')
  expect(state.signIn).not.toHaveBeenCalled()
})
