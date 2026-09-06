import { relayHttpUrl } from './origin'

/**
 * The relay's occupancy map for one session, behind the same bearer as the
 * relay's copy of the media configuration. Absent means the console was given
 * no relay bootstrap, so no map can be read and none is invented.
 */
export interface MapEndpoint {
  /** GET: an 8-bit grayscale PNG with the X-Sweep-Map-* headers. */
  url: string
  /** POST: clears the session's occupancy grid. */
  resetUrl: string
  authorization: string
}

/** Null unless the bootstrap gives an HTTP-capable relay URL, a session, and a token. */
export function relayMapEndpoint(
  baseUrl: string,
  sessionId: string,
  token: string,
): MapEndpoint | null {
  if (!sessionId || !token) return null
  const path = `/api/sessions/${encodeURIComponent(sessionId)}/map`
  const url = relayHttpUrl(baseUrl, path)
  const resetUrl = relayHttpUrl(baseUrl, `${path}/reset`)
  if (!url || !resetUrl) return null
  return { url, resetUrl, authorization: `Bearer ${token}` }
}
