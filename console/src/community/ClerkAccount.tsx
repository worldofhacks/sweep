import { ClerkProvider, SignInButton, UserButton, useUser } from '@clerk/react'

/** Clerk owns provider redirects, account linking, session lifecycle and sign-out. */
export default function ClerkAccount({ publishableKey }: { publishableKey: string }) {
  return <ClerkProvider publishableKey={publishableKey} appearance={{ variables: {
    colorPrimary: '#087eaa', colorBackground: '#fffefa', colorForeground: '#203944',
    borderRadius: '12px', fontFamily: 'Public Sans, system-ui, sans-serif',
  } }}><Identity /></ClerkProvider>
}

function Identity() {
  const { isLoaded, isSignedIn, user } = useUser()
  if (!isLoaded) return <span role="status">Opening sign-in…</span>
  if (isSignedIn) return <div className="community-signed-in"><span>{user.firstName || 'Your account'}</span><UserButton /></div>
  return <SignInButton mode="modal" forceRedirectUrl={window.location.origin + window.location.pathname}>
    <button className="community-account">Join in / Sign in</button>
  </SignInButton>
}
