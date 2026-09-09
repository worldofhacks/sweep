import { useSyncExternalStore } from 'react'

export interface AccountSession {
  key: string
  userId: string
  getToken: () => Promise<string | null>
}

// A transient bridge from the isolated header SDK. No token is copied into storage,
// runtime bootstrap, invitations, or operator clients. Native never installs a session.
let session: AccountSession | null = null
const listeners = new Set<() => void>()
export const currentAccountSession = () => session
export function publishAccountSession(value: AccountSession | null) {
  session = value
  listeners.forEach(listener => listener())
}
function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
export function useAccountSession() {
  return useSyncExternalStore(subscribe, currentAccountSession, () => null)
}
