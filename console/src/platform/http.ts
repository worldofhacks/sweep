import { relayHttpUrl } from '../relay/origin'

export interface PlatformConnection { baseUrl: string; sessionId: string; token: string }
export type PlatformFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export class PlatformHttp {
  readonly connection: PlatformConnection
  private readonly fetcher: PlatformFetch
  constructor(connection: PlatformConnection, fetcher: PlatformFetch = (input, init) => globalThis.fetch(input, init)) {
    this.connection = Object.freeze({ ...connection })
    this.fetcher = fetcher
  }

  async request(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const url = relayHttpUrl(this.connection.baseUrl, `/api/sessions/${encodeURIComponent(this.connection.sessionId)}${path}`)
    if (!url) throw new Error('The relay URL is invalid.')
    const controller = new AbortController()
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) controller.abort()
    const timeout = setTimeout(abort, 10_000)
    try {
      const response = await this.fetcher(url, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${this.connection.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
      })
      const maximum = 16 * 1024 * 1024
      const length = Number(response.headers.get('Content-Length'))
      if (Number.isFinite(length) && length > maximum) throw new Error('The relay response exceeds its size limit.')
      const reader = response.body?.getReader()
      let value: unknown
      if (reader) {
        const decoder = new TextDecoder('utf-8', { fatal: true })
        let size = 0, raw = ''
        try {
          while (true) {
            const part = await reader.read()
            if (part.done) break
            size += part.value.byteLength
            if (size > maximum) throw new Error('The relay response exceeds its size limit.')
            raw += decoder.decode(part.value, { stream: true })
          }
          value = JSON.parse(raw + decoder.decode())
        } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
      } else {
        throw new Error('The relay returned an empty response.')
      }
      if (!response.ok) {
        const detail = isRecord(value) && typeof value.detail === 'string' && value.detail.length <= 2048 ? value.detail : `Relay service unavailable (${response.status}).`
        throw new Error(detail)
      }
      return value
    } finally { clearTimeout(timeout); signal?.removeEventListener('abort', abort) }
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
