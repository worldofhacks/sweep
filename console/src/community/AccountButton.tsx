import { Component, lazy, Suspense, type ReactNode } from 'react'
import { AccountSetup } from './AccountSetup'

const ClerkAccount = lazy(() => import('./ClerkAccount'))

/** Web only. No auth network traffic without configuration. */
export function AccountButton() {
  const key = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined
  if (key) return <AccountBoundary><Suspense fallback={<span role="status">Opening sign-in…</span>}>
    <ClerkAccount publishableKey={key} />
  </Suspense></AccountBoundary>
  return <AccountSetup />
}

class AccountBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <span className="community-account-error" role="status">Sign-in unavailable. You can keep exploring.</span> : this.props.children
  }
}
