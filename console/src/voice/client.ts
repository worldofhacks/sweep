/**
 * Transcript upload client for the relay push-to-talk endpoint.
 * Copied from PR #49 (issue-42-push-to-talk, console/src/voice/client.ts) so the
 * Speech module binds to the transcription contract that branch defines.
 */
export type VoiceOutcome = {
  v?: 1
  type?: 'voice_outcome'
  session?: string
  correlation_id?: string
  status: 'transcribed' | 'refused'
  source: 'whisper' | 'template'
  reason: string | null
  transcript: string | null
  emissions: []
}

export type TranscriptRequest = {
  sessionId: string
  correlationId: string
  audio: Blob
  durationMs: number
}

export interface TranscriptClient {
  transcribe(request: TranscriptRequest): Promise<VoiceOutcome>
}

export type HttpTranscriptClientConfig = {
  baseUrl: string
  token: string
}

type Fetcher = typeof fetch

export class HttpTranscriptClient implements TranscriptClient {
  private readonly config: HttpTranscriptClientConfig
  private readonly fetcher: Fetcher

  constructor(config: HttpTranscriptClientConfig, fetcher: Fetcher = fetch) {
    this.config = config
    this.fetcher = fetcher
  }

  async transcribe(request: TranscriptRequest): Promise<VoiceOutcome> {
    const contentType = request.audio.type || 'audio/webm'
    // Native Window.fetch rejects when it is invoked as an instance method and
    // receives this client as its receiver. Copy it first so both the browser
    // implementation and injected test fetchers are called as plain functions.
    const fetcher = this.fetcher
    const response = await fetcher(transcriptEndpoint(this.config.baseUrl, request.sessionId), {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.config.token}`,
        'Content-Type': contentType,
        'X-Sweep-Correlation-Id': request.correlationId,
        'X-Sweep-Audio-Duration-Ms': String(request.durationMs),
      },
      body: request.audio,
    })
    if (!response.ok) throw new Error(responseFailure(response.status))
    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      throw new Error('Voice relay returned an invalid response.')
    }
    if (!isVoiceOutcome(payload, request.sessionId, request.correlationId)) {
      throw new Error('Voice relay returned an invalid response.')
    }
    return payload
  }
}

function responseFailure(status: number): string {
  if (status === 400) return 'Voice request was rejected by the relay.'
  if (status === 401) return 'Voice relay authentication failed.'
  // This console can talk to a relay that has no transcription endpoint yet; say so rather than "failed".
  if (status === 404 || status === 405) return 'The relay has no transcription endpoint. Nothing was emitted.'
  if (status === 413) return 'Voice recording exceeds the relay upload limit.'
  return 'Voice relay request failed.'
}

export class UnavailableTranscriptClient implements TranscriptClient {
  private readonly reason: string

  constructor(reason: string) {
    this.reason = reason
  }

  async transcribe(request: TranscriptRequest): Promise<VoiceOutcome> {
    void request
    throw new Error(this.reason)
  }
}

export function transcriptEndpoint(baseUrl: string, sessionId: string): string {
  const url = new URL(baseUrl)
  if (url.protocol === 'ws:') url.protocol = 'http:'
  if (url.protocol === 'wss:') url.protocol = 'https:'
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error('Relay URL must use ws, wss, http, or https.')
  }
  url.username = ''
  url.password = ''
  url.search = ''
  url.hash = ''
  const basePath = url.pathname.replace(/\/$/, '')
  url.pathname = `${basePath}/api/sessions/${encodeURIComponent(sessionId)}/transcripts`
  return url.toString()
}

function isVoiceOutcome(value: unknown, sessionId: string, correlationId: string): value is VoiceOutcome {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const record = value as Record<string, unknown>
  return (
    record.v === 1 &&
    record.type === 'voice_outcome' &&
    record.session === sessionId &&
    record.correlation_id === correlationId &&
    (record.status === 'transcribed' || record.status === 'refused') &&
    (record.source === 'whisper' || record.source === 'template') &&
    (record.reason === null || typeof record.reason === 'string') &&
    (record.transcript === null || typeof record.transcript === 'string') &&
    Array.isArray(record.emissions) &&
    record.emissions.length === 0
  )
}
