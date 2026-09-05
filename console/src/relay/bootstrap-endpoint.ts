/**
 * The relay bootstrap endpoint, shared by the development server plugin in
 * vite.config.ts that serves it and the browser loader in bootstrap.ts that
 * reads it. It follows the media runtime configuration: same-origin JSON built
 * from the environment at request time, so the token never enters the bundle.
 * This module stays free of browser and Node types because both sides load it.
 */

export const RELAY_BOOTSTRAP_ENDPOINT = '/relay-bootstrap.json'
export const DEFAULT_RELAY_ORIGIN = 'ws://127.0.0.1:8000'
export const DEFAULT_RELAY_SESSION_ID = 'demo'

export interface RelayBootstrap {
  baseUrl: string
  sessionId: string
  token: string
}

/**
 * Builds the endpoint payload from the relay's own variable names, so one
 * `.env` serves both processes. Without a token there is no bootstrap: the
 * endpoint answers 503 and the console stays visibly disconnected.
 */
export function relayFromEnvironment(
  env: Readonly<Record<string, string | undefined>>,
): RelayBootstrap | null {
  const token = env.SWEEP_RELAY_TOKEN
  if (!token) return null
  return {
    baseUrl: env.SWEEP_RELAY_ORIGIN || DEFAULT_RELAY_ORIGIN,
    sessionId: env.SWEEP_SESSION_ID || DEFAULT_RELAY_SESSION_ID,
    token,
  }
}
