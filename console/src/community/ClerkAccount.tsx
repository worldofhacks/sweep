import { ClerkProvider, SignInButton, UserButton, useUser, useAuth } from '@clerk/react'
import { useLayoutEffect } from 'react'
import { publishAccountSession } from './accountSession'

/** Clerk owns provider redirects, account linking, session lifecycle and sign-out. */
export default function ClerkAccount({ publishableKey }: { publishableKey: string }) {
  return <ClerkProvider publishableKey={publishableKey} appearance={{ variables: {
    colorPrimary: '#087eaa', colorBackground: '#fffefa', colorForeground: '#203944',
    borderRadius: '12px', fontFamily: 'Public Sans, system-ui, sans-serif',
  } }}><Identity /></ClerkProvider>
}

function Identity() {
  const { isLoaded, isSignedIn, user } = useUser()
  const { userId, sessionId, getToken } = useAuth()
  useLayoutEffect(() => {
    publishAccountSession(isLoaded && isSignedIn && userId && sessionId
      ? { key: `${userId}:${sessionId}`, userId, getToken } : null)
    return () => publishAccountSession(null)
  }, [isLoaded, isSignedIn, userId, sessionId, getToken])
  if (!isLoaded) return <span role="status">Opening sign-in…</span>
  if (isSignedIn) return <div className="community-signed-in"><span>{user.firstName || 'Your account'}</span><UserButton /></div>
  return <SignInButton mode="modal" forceRedirectUrl={window.location.origin + window.location.pathname}>
    <button className="community-account">Join in / Sign in</button>
  </SignInButton>
}
