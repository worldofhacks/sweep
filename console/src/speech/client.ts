export interface LanguageIntent { name: string; args: Record<string, unknown>; selection: number[] }
export interface LanguageCompilation { kind: 'plan' | 'clarify' | 'refuse' | 'unsupported' | 'cancel_pending'; source: string; reason: string | null; detail: string | null; expires_at_ms?: number | null; intents: LanguageIntent[] }
export interface LanguageClient { compile(text: string, correlationId: string): Promise<LanguageCompilation> }
export class HttpLanguageClient implements LanguageClient {
  private readonly baseUrl: string
  private readonly sessionId: string
  private readonly token: string
  private readonly fetcher: typeof fetch
  constructor(baseUrl: string, sessionId: string, token: string, fetcher: typeof fetch = fetch) { this.baseUrl = baseUrl; this.sessionId = sessionId; this.token = token; this.fetcher = fetcher }
  async compile(text: string, correlationId: string): Promise<LanguageCompilation> {
    const url = new URL(this.baseUrl); url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:'
    url.pathname = `${url.pathname.replace(/\/$/, '')}/api/sessions/${encodeURIComponent(this.sessionId)}/compile`
    const response = await this.fetcher(url, { method: 'POST', headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ text, correlation_id: correlationId }) })
    if (!response.ok) throw new Error('Language compilation is unavailable.')
    const body: unknown = await response.json()
    const compilation = body && typeof body === 'object' ? (body as { compilation?: unknown }).compilation : null
    if (!compilation || typeof compilation !== 'object' || Array.isArray(compilation)) throw new Error('Language relay returned an invalid response.')
    const value = compilation as Partial<LanguageCompilation>
    if (!['plan', 'clarify', 'refuse', 'unsupported', 'cancel_pending'].includes(String(value.kind)) || !Array.isArray(value.intents)) throw new Error('Language relay returned an invalid response.')
    return value as LanguageCompilation
  }
}
