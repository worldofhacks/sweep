import { relayHttpUrl } from './origin'

/**
 * The relay's occupancy map for one session, behind the same bearer as the
 * relay's copy of the media configuration. Absent means the console was given
 * no relay bootstrap, so no map can be read and none is invented.
 */
export interface MapEndpoint {
  /** GET: an 8-bit grayscale PNG with the X-Sweep-Map-* headers. */
  url: string
  /** An explicitly supported POST reset endpoint; absent in the production bootstrap. */
  resetUrl?: string
  authorization: string
  /** Injected fixture transport; real endpoints use the browser fetch. */
  fetcher?: typeof fetch
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
  if (!url) return null
  return { url, authorization: `Bearer ${token}` }
}
