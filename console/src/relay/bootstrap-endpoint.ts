/**
 * The relay bootstrap endpoint, shared by the development server plugin in
 * vite.config.ts that serves it and the browser loader in bootstrap.ts that
 * reads it. It follows the media runtime configuration: same-origin JSON built
 * from the environment at request time, so the token never enters the bundle.
 * This module stays free of browser and Node types because both sides load it.
 */

export const RELAY_BOOTSTRAP_ENDPOINT = '/relay-bootstrap.json'

export interface RelayBootstrap {
  baseUrl: string
  sessionId: string
  token: string
}

/**
 * Builds the endpoint payload from the relay's own variable names, so one
 * `.env` serves both processes. Without an explicit origin, session and token, the
 * endpoint answers 503 and the console stays visibly disconnected.
 */
export function relayFromEnvironment(
  env: Readonly<Record<string, string | undefined>>,
): RelayBootstrap | null {
  const token = env.SWEEP_RELAY_TOKEN
  const baseUrl = env.SWEEP_RELAY_ORIGIN
  const sessionId = env.SWEEP_SESSION_ID
  if (!token?.trim() || !baseUrl?.trim() || !sessionId?.trim()) return null
  if (baseUrl !== baseUrl.trim() || sessionId !== sessionId.trim()) return null
  try {
    const url = new URL(baseUrl)
    if (!['ws:', 'wss:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null
  } catch {
    return null
  }
  return { baseUrl, sessionId, token }
}
