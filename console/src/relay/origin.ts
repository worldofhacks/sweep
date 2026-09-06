/**
 * The relay serves its bearer-guarded HTTP endpoints on the bootstrap's
 * WebSocket URL read as HTTP. One place converts that URL so the transcripts
 * endpoint, the media configuration and the occupancy map agree on what the
 * relay's HTTP base is: the same host and port, the relay's own base path,
 * and no credentials, query or fragment.
 */
export function relayHttpBase(baseUrl: string): URL | null {
  let url: URL
  try {
    url = new URL(baseUrl)
  } catch {
    return null
  }
  if (url.protocol === 'ws:') url.protocol = 'http:'
  if (url.protocol === 'wss:') url.protocol = 'https:'
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  url.username = ''
  url.password = ''
  url.search = ''
  url.hash = ''
  return url
}

/** Host and port only, for endpoints the relay serves at the root. */
export function relayHttpOrigin(baseUrl: string): string | null {
  return relayHttpBase(baseUrl)?.origin ?? null
}

/**
 * An endpoint under the relay's base path; `path` starts with a slash. A
 * pathname is never empty, so the relay's own trailing slash is dropped first.
 */
export function relayHttpUrl(baseUrl: string, path: string): string | null {
  const url = relayHttpBase(baseUrl)
  if (!url) return null
  url.pathname = `${url.pathname.replace(/\/$/, '')}${path}`
  return url.toString()
}
