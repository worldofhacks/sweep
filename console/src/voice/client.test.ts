// Copied from PR #49 (issue-42-push-to-talk, console/src/voice/client.test.ts),
// extended with the versioned `plan` field.
import { describe, expect, test, vi } from 'vitest'
import type { VoicePlan } from '../relay/contract'
import { HttpTranscriptClient, UnavailableTranscriptClient, isVoiceOutcome } from './client'

function compiledPlan(overrides: Partial<VoicePlan> = {}): VoicePlan {
  return {
    v: 1,
    kind: 'plan',
    transcript: 'Take off.',
    reason: null,
    detail: null,
    options: [],
    steps: [
      {
        index: 0,
        intent_id: 'voice-step-0',
        name: 'takeoff',
        args: {},
        selection: [1],
        mode: 'indoor',
        confirm_required: true,
        notes: ['Targets D-01 (the current selection).'],
      },
    ],
    compiled_at_ms: 1_756_700_000_000,
    expires_at_ms: 1_756_700_030_000,
    state_event_id: 'state-event-9',
    roster_version: 7,
    session: 'session-1',
    correlation_id: 'voice-plan',
    plan_digest: 'a'.repeat(64),
    model: 'claude-sonnet-5',
    prompt_schema_version: 'intent-v1-compiler-8',
    response_source: 'anthropic',
    pending_intent_id: null,
    ...overrides,
  }
}

function outcomeWith(plan: unknown, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    v: 1,
    type: 'voice_outcome',
    session: 'session-1',
    correlation_id: 'voice-plan',
    status: 'transcribed',
    source: 'whisper',
    reason: null,
    transcript: 'Take off.',
    emissions: [],
    plan,
    ...overrides,
  }
}

describe('voice outcome validator', () => {
  test('accepts the original shape with plan absent or null', () => {
    const legacy: Record<string, unknown> = outcomeWith(null)
    delete legacy.plan
    expect(isVoiceOutcome(legacy, 'session-1', 'voice-plan')).toBe(true)
    expect(isVoiceOutcome(outcomeWith(null), 'session-1', 'voice-plan')).toBe(true)
  })

  test('accepts a compiled plan, a clarification with options, and a typed refusal', () => {
    expect(isVoiceOutcome(outcomeWith(compiledPlan()), 'session-1', 'voice-plan')).toBe(true)
    const clarify = compiledPlan({
      kind: 'clarify',
      reason: 'ambiguous_location',
      options: ['living-room', 'bedroom'],
      steps: [],
      expires_at_ms: null,
      plan_digest: null,
      transcript: 'Capture this room.',
    })
    expect(isVoiceOutcome(outcomeWith(clarify, { transcript: 'Capture this room.' }), 'session-1', 'voice-plan')).toBe(true)
    const refuse = compiledPlan({
      kind: 'refuse',
      reason: 'invalid_model_output',
      detail: 'The proposed plan did not pass deterministic validation.',
      steps: [],
      expires_at_ms: null,
      plan_digest: null,
    })
    expect(isVoiceOutcome(outcomeWith(refuse), 'session-1', 'voice-plan')).toBe(true)
  })

  test('rejects a plan that is widened, unbound, or carries an emission', () => {
    const valid = compiledPlan()
    expect(isVoiceOutcome(outcomeWith(valid, { emissions: [{ name: 'takeoff' }] }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith(valid, { status: 'refused' }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, correlation_id: 'other' }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, session: 'other' }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, transcript: 'Land.' }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, emit: true }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, v: 2 }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, steps: [] }), 'session-1', 'voice-plan')).toBe(false)
    expect(isVoiceOutcome(outcomeWith({ ...valid, expires_at_ms: valid.compiled_at_ms }), 'session-1', 'voice-plan')).toBe(false)
    expect(
      isVoiceOutcome(outcomeWith({ ...valid, steps: [{ ...valid.steps[0], name: 'map_area' }] }), 'session-1', 'voice-plan'),
    ).toBe(false)
    expect(
      isVoiceOutcome(outcomeWith({ ...valid, steps: [{ ...valid.steps[0], index: 1 }] }), 'session-1', 'voice-plan'),
    ).toBe(false)
    expect(
      isVoiceOutcome(
        outcomeWith({ ...valid, steps: [{ ...valid.steps[0], args: { z: Number.POSITIVE_INFINITY } }] }),
        'session-1',
        'voice-plan',
      ),
    ).toBe(false)
    expect(
      isVoiceOutcome(
        outcomeWith({ ...valid, kind: 'refuse', reason: 'stale_state' }),
        'session-1',
        'voice-plan',
      ),
    ).toBe(false)
    expect(
      isVoiceOutcome(
        outcomeWith({ ...valid, kind: 'clarify', steps: [], expires_at_ms: null, plan_digest: null }),
        'session-1',
        'voice-plan',
      ),
    ).toBe(false)
  })

  test('the client hands a compiled plan through untouched and never an emission', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(outcomeWith(compiledPlan())), { status: 200 }))
    const client = new HttpTranscriptClient({ baseUrl: 'ws://relay.example', token: 'relay-token' }, fetcher)

    const outcome = await client.transcribe({
      sessionId: 'session-1',
      correlationId: 'voice-plan',
      audio: new Blob(['audio'], { type: 'audio/webm' }),
      durationMs: 900,
    })

    expect(outcome.emissions).toEqual([])
    expect(outcome.plan).toEqual(compiledPlan())
  })
})

describe('transcript upload client', () => {
  test('calls the browser fetch implementation without rebinding its receiver', async () => {
    const fetcher = function (this: unknown) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            v: 1,
            type: 'voice_outcome',
            session: 'session-1',
            correlation_id: 'voice-browser',
            status: 'transcribed',
            source: 'whisper',
            reason: null,
            transcript: 'hold position',
            emissions: [],
          }),
          { status: 200 },
        ),
      )
    }
    const trackedFetcher = vi.fn(fetcher)
    const client = new HttpTranscriptClient(
      { baseUrl: 'ws://relay.example', token: 'relay-token' },
      trackedFetcher as unknown as typeof fetch,
    )

    await client.transcribe({
      sessionId: 'session-1',
      correlationId: 'voice-browser',
      audio: new Blob(['audio'], { type: 'audio/webm' }),
      durationMs: 500,
    })

    expect(trackedFetcher).toHaveBeenCalledTimes(1)
    expect(trackedFetcher.mock.contexts[0]).toBeUndefined()
  })

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
