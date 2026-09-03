import type { MediaRuntimeConfiguration } from './media/playback'

const endpoint = '/runtime-config.json'

export function bootstrapMediaConfiguration(
  renderConsole: (configuration?: MediaRuntimeConfiguration) => void,
  loader: () => Promise<MediaRuntimeConfiguration | undefined> = loadMediaRuntimeConfiguration,
): void {
  renderConsole()
  void loader().then((configuration) => {
    if (configuration) renderConsole(configuration)
  })
}

export async function loadMediaRuntimeConfiguration(
  fetcher: typeof fetch = fetch,
  report: (message: string) => void = console.warn,
): Promise<MediaRuntimeConfiguration | undefined> {
  try {
    const response = await fetcher(endpoint, { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) return unavailable(report)
    const payload: unknown = await response.json()
    if (!isObject(payload)) {
      return unavailable(report)
    }
    const configuration = normalizeMediaConfiguration(payload.media)
    return configuration ?? unavailable(report)
  } catch {
    return unavailable(report)
  }
}

function unavailable(report: (message: string) => void): undefined {
  report('Media playback disabled: runtime configuration unavailable.')
}

function normalizeMediaConfiguration(value: unknown): MediaRuntimeConfiguration | undefined {
  if (!isObject(value)) return undefined
  const readerUsername = nonemptyString(value.readerUsername)
  const readerPassword = nonemptyString(value.readerPassword)
  if (!readerUsername || !readerPassword) return undefined
  try {
    return {
      webrtcOrigin: normalizeOrigin(value.webrtcOrigin),
      hlsOrigin: normalizeOrigin(value.hlsOrigin),
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
