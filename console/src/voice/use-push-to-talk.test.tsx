import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import type { TranscriptClient } from './client'
import {
  MAX_RECORDING_MS,
  RECORDING_ENCODING_MARGIN_MS,
  RELAY_MAX_AUDIO_DURATION_MS,
  usePushToTalk,
  type RecorderFactory,
} from './use-push-to-talk'

afterEach(() => vi.useRealTimers())

class FakeRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: BlobEvent) => void) | null = null
  onstop: (() => void) | null = null
  onerror: ((event: ErrorEvent) => void) | null = null
  start = vi.fn(() => {
    this.state = 'recording'
  })

  stop = vi.fn(() => {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['recording'], { type: this.mimeType }) } as BlobEvent)
    this.onstop?.()
  })
}

describe('push-to-talk recording', () => {
  test('stops one encoding margin before the relay duration cap', async () => {
    vi.useFakeTimers()
    const recorder = new FakeRecorder()
    const stopTrack = vi.fn()
    const client: TranscriptClient = {
      transcribe: vi.fn().mockResolvedValue({
        status: 'refused',
        source: 'template',
        reason: 'compiler_unavailable',
        transcript: 'hold selected aircraft',
        emissions: [],
      }),
    }
    const recorderFactory: RecorderFactory = () => recorder
    const { result } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: async () => ({ getTracks: () => [{ stop: stopTrack }] }) as unknown as MediaStream,
        recorderFactory,
        nextId: () => 'voice-1',
      }),
    )

    await act(async () => result.current.start())
    expect(result.current.status).toBe('recording')
    expect(MAX_RECORDING_MS).toBe(RELAY_MAX_AUDIO_DURATION_MS - RECORDING_ENCODING_MARGIN_MS)
    expect(MAX_RECORDING_MS).toBe(29_000)
    await act(async () => vi.advanceTimersByTimeAsync(MAX_RECORDING_MS))

    expect(recorder.stop).toHaveBeenCalledTimes(1)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(client.transcribe).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'session-1', correlationId: 'voice-1' }),
    )
    expect(result.current.outcome?.reason).toBe('compiler_unavailable')
  })

  test('cleans up a recorder error and never uploads partial audio', async () => {
    const recorder = new FakeRecorder()
    const stopTrack = vi.fn()
    const client: TranscriptClient = { transcribe: vi.fn() }
    const { result } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: async () => ({ getTracks: () => [{ stop: stopTrack }] }) as unknown as MediaStream,
        recorderFactory: (() => recorder) as RecorderFactory,
      }),
    )

    await act(async () => result.current.start())
    act(() => recorder.onerror?.({} as ErrorEvent))

    expect(result.current.status).toBe('error')
    expect(result.current.detail).toBe('Recording failed. No audio was sent.')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(client.transcribe).not.toHaveBeenCalled()
  })

  test('cleans up a recorder start failure without uploading audio', async () => {
    const recorder = new FakeRecorder()
    recorder.start.mockImplementation(() => {
      throw new Error('Recorder start failed')
    })
    const stopTrack = vi.fn()
    const client: TranscriptClient = { transcribe: vi.fn() }
    const { result } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: async () => ({ getTracks: () => [{ stop: stopTrack }] }) as unknown as MediaStream,
        recorderFactory: (() => recorder) as RecorderFactory,
      }),
    )

    await act(async () => result.current.start())

    expect(result.current.status).toBe('error')
    expect(result.current.detail).toBe('Recording could not start. No audio was sent.')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(client.transcribe).not.toHaveBeenCalled()
  })

  test('reports a microphone permission failure without an upload', async () => {
    const client: TranscriptClient = { transcribe: vi.fn() }
    const { result } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: async () => {
          throw new Error('Permission denied')
        },
        recorderFactory: (() => new FakeRecorder()) as RecorderFactory,
      }),
    )

    await act(async () => result.current.start())

    expect(result.current.status).toBe('error')
    expect(result.current.detail).toBe('Microphone access was not granted. No audio was sent.')
    expect(client.transcribe).not.toHaveBeenCalled()
  })

  test('release before microphone permission resolves discards the stream', async () => {
    let resolveStream: ((stream: MediaStream) => void) | undefined
    const pendingStream = new Promise<MediaStream>((resolve) => {
      resolveStream = resolve
    })
    const stopTrack = vi.fn()
    const recorderFactory = vi.fn(() => new FakeRecorder())
    const client: TranscriptClient = { transcribe: vi.fn() }
    const { result } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: () => pendingStream,
        recorderFactory,
      }),
    )

    act(() => {
      void result.current.start()
    })
    act(() => result.current.stop())
    await act(async () => {
      resolveStream?.({ getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream)
    })

    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(recorderFactory).not.toHaveBeenCalled()
    expect(client.transcribe).not.toHaveBeenCalled()
  })

  test('discards an active recording when the control unmounts', async () => {
    const recorder = new FakeRecorder()
    const stopTrack = vi.fn()
    const client: TranscriptClient = { transcribe: vi.fn() }
    const { result, unmount } = renderHook(() =>
      usePushToTalk({
        sessionId: 'session-1',
        client,
        requestAudio: async () => ({ getTracks: () => [{ stop: stopTrack }] }) as unknown as MediaStream,
        recorderFactory: (() => recorder) as RecorderFactory,
      }),
    )

    await act(async () => result.current.start())
    unmount()

    expect(recorder.stop).toHaveBeenCalledTimes(1)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(client.transcribe).not.toHaveBeenCalled()
  })
})
