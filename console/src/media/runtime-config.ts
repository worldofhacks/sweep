/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/runtime-config.ts).
 * The HLS origin is no longer required because only WHEP playback is carried
 * over; an `hlsOrigin` in the payload is ignored. Reconcile when #68 merges.
 */
import type { MediaRuntimeConfiguration } from './playback'

export const MEDIA_CONFIG_ENDPOINT = '/runtime-config.json'

/** One place the media bootstrap may be read from; sources are tried in order. */
export interface MediaConfigurationSource {
  url: string
  /** The relay bearer for the relay's copy of the endpoint; absent for the same-origin one. */
  authorization?: string
}

/** The development server's endpoint, or whatever a production host serves at the same path. */
export const SAME_ORIGIN_MEDIA_SOURCE: MediaConfigurationSource = { url: MEDIA_CONFIG_ENDPOINT }

/**
 * The relay serves the same JSON at `/runtime-config.json` behind its bearer token
 * (relay/README.md), so a built console can play without a host that proxies the endpoint.
 * The HTTP origin is the relay bootstrap's WebSocket origin; anything else yields no source.
 */
export function relayMediaConfigurationSource(
  baseUrl: string,
  token: string,
): MediaConfigurationSource | null {
  let url: URL
  try {
    url = new URL(baseUrl)
  } catch {
    return null
  }
  if (url.protocol === 'ws:') url.protocol = 'http:'
  if (url.protocol === 'wss:') url.protocol = 'https:'
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !token) return null
  return {
    url: new URL(MEDIA_CONFIG_ENDPOINT, url.origin).toString(),
    authorization: `Bearer ${token}`,
  }
}

/**
 * Renders the console immediately without media, then once more with the
 * media configuration when the runtime endpoint provides a valid one.
 */
export function bootstrapMediaConfiguration(
  renderConsole: (configuration?: MediaRuntimeConfiguration) => void,
  loader: () => Promise<MediaRuntimeConfiguration | undefined> = loadMediaRuntimeConfiguration,
): void {
  renderConsole()
  void loader().then((configuration) => {
    if (configuration) renderConsole(configuration)
  })
}

/** The first source that answers with a complete configuration wins; none means no playback. */
export async function loadMediaRuntimeConfiguration(
  fetcher: typeof fetch = fetch,
  report: (message: string) => void = console.warn,
  sources: readonly MediaConfigurationSource[] = [SAME_ORIGIN_MEDIA_SOURCE],
): Promise<MediaRuntimeConfiguration | undefined> {
  for (const source of sources) {
    const configuration = await readSource(fetcher, source)
    if (configuration) return configuration
  }
  return unavailable(report)
}

async function readSource(
  fetcher: typeof fetch,
  source: MediaConfigurationSource,
): Promise<MediaRuntimeConfiguration | undefined> {
  try {
    const response = await fetcher(
      source.url,
      source.authorization
        ? { cache: 'no-store', credentials: 'omit', headers: { Authorization: source.authorization } }
        : { cache: 'no-store', credentials: 'same-origin' },
    )
    if (!response.ok) return undefined
    const payload: unknown = await response.json()
    if (!isObject(payload)) return undefined
    return normalizeMediaConfiguration(payload.media)
  } catch {
    return undefined
  }
}

function unavailable(report: (message: string) => void): undefined {
  report('Media playback disabled: runtime configuration unavailable.')
}

/** Accepts only a complete configuration; credentials are never defaulted. */
export function normalizeMediaConfiguration(value: unknown): MediaRuntimeConfiguration | undefined {
  if (!isObject(value)) return undefined
  const readerUsername = nonemptyString(value.readerUsername)
  const readerPassword = nonemptyString(value.readerPassword)
  if (!readerUsername || !readerPassword) return undefined
  try {
    return {
      webrtcOrigin: normalizeOrigin(value.webrtcOrigin),
      readerUsername,
      readerPassword,
    }
  } catch {
    return undefined
  }
}

function normalizeOrigin(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error('origin is required')
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('origin must use HTTP or HTTPS without credentials')
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error('origin must not contain a path, query, or fragment')
  }
  return url.origin
}

function nonemptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
