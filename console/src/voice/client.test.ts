// Copied from PR #49 (issue-42-push-to-talk, console/src/voice/client.test.ts).
import { describe, expect, test, vi } from 'vitest'
import { HttpTranscriptClient, UnavailableTranscriptClient } from './client'

describe('transcript upload client', () => {
  test('sends bounded recorded audio to the authenticated relay endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          v: 1,
          type: 'voice_outcome',
          session: 'session-1',
          correlation_id: 'voice-1',
          status: 'refused',
          source: 'template',
          reason: 'compiler_unavailable',
          transcript: 'hold selected aircraft',
          emissions: [],
        }),
        { status: 200 },
      ),
    )
    const client = new HttpTranscriptClient(
      { baseUrl: 'wss://relay.example/internal', token: 'relay-token' },
      fetcher,
    )

    const outcome = await client.transcribe({
      sessionId: 'session-1',
      correlationId: 'voice-1',
      audio: new Blob(['audio'], { type: 'audio/webm' }),
      durationMs: 1_250,
    })

    expect(outcome.reason).toBe('compiler_unavailable')
    expect(outcome.emissions).toEqual([])
    expect(fetcher).toHaveBeenCalledWith(
      'https://relay.example/internal/api/sessions/session-1/transcripts',
      expect.objectContaining({
        method: 'POST',
        headers: {
          Authorization: 'Bearer relay-token',
          'Content-Type': 'audio/webm',
          'X-Sweep-Correlation-Id': 'voice-1',
          'X-Sweep-Audio-Duration-Ms': '1250',
        },
      }),
    )
  })

  test('does not turn malformed relay responses into a local command', async () => {
    const client = new HttpTranscriptClient(
      { baseUrl: 'ws://relay.example', token: 'relay-token' },
      vi.fn().mockResolvedValue(new Response('{"status":"transcribed"}', { status: 200 })),
    )

    await expect(
      client.transcribe({
        sessionId: 'session-1',
        correlationId: 'voice-2',
        audio: new Blob(['audio'], { type: 'audio/webm' }),
        durationMs: 1_250,
      }),
    ).rejects.toThrow('Voice relay returned an invalid response.')
  })

  test.each([
    [400, 'Voice request was rejected by the relay.'],
    [401, 'Voice relay authentication failed.'],
    [404, 'The relay has no transcription endpoint. Nothing was emitted.'],
    [413, 'Voice recording exceeds the relay upload limit.'],
    [500, 'Voice relay request failed.'],
  ])('reports HTTP %i without retrying', async (status, message) => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"detail":"refused"}', { status }))
    const client = new HttpTranscriptClient(
      { baseUrl: 'ws://relay.example', token: 'relay-token' },
      fetcher,
    )

    await expect(
      client.transcribe({
        sessionId: 'session-1',
        correlationId: 'voice-status',
        audio: new Blob(['audio'], { type: 'audio/webm' }),
        durationMs: 1_250,
      }),
    ).rejects.toThrow(message)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  test('unconfigured runtime rejects before any upload attempt', async () => {
    await expect(
      new UnavailableTranscriptClient('Voice relay bootstrap is unavailable.').transcribe({
        sessionId: 'session-1',
        correlationId: 'voice-3',
        audio: new Blob(['audio'], { type: 'audio/webm' }),
        durationMs: 1_250,
      }),
    ).rejects.toThrow('Voice relay bootstrap is unavailable.')
  })
})
