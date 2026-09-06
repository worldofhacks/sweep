/**
 * Bounded push-to-talk recorder: hold to record, release to upload one blob.
 * Copied from PR #49 (issue-42-push-to-talk, console/src/voice/use-push-to-talk.ts);
 * this copy adds an injectable clock and exposes the recording start time so the
 * Speech module can count down to the cap deterministically.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { TranscriptClient, VoiceOutcome } from './client'

export const RELAY_MAX_AUDIO_DURATION_MS = 30_000
export const RECORDING_ENCODING_MARGIN_MS = 1_000
export const MAX_RECORDING_MS = RELAY_MAX_AUDIO_DURATION_MS - RECORDING_ENCODING_MARGIN_MS

type Recorder = {
  state: 'inactive' | 'recording' | 'paused'
  mimeType: string
  ondataavailable: ((event: BlobEvent) => void) | null
  onstop: ((event: Event) => void) | null
  onerror: ((event: ErrorEvent) => void) | null
  start(): void
  stop(): void
}

export type RecorderFactory = (stream: MediaStream) => Recorder

export type PushToTalkStatus =
  | 'idle'
  | 'requesting_microphone'
  | 'recording'
  | 'uploading'
  | 'transcribed'
  | 'refused'
  | 'error'

export interface UsePushToTalkOptions {
  sessionId: string
  client: TranscriptClient
  requestAudio?: () => Promise<MediaStream>
  recorderFactory?: RecorderFactory
  nextId?: () => string
  maxRecordingMs?: number
  /** Wall clock for the recording start and duration; injected so the countdown is testable. */
  now?: () => number
}

export function usePushToTalk({
  sessionId,
  client,
  requestAudio = () => navigator.mediaDevices.getUserMedia({ audio: true }),
  recorderFactory = (stream) => new MediaRecorder(stream),
  nextId = () => crypto.randomUUID(),
  maxRecordingMs = MAX_RECORDING_MS,
  now = () => Date.now(),
}: UsePushToTalkOptions) {
  const [status, setStatus] = useState<PushToTalkStatus>('idle')
  const [detail, setDetail] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<VoiceOutcome | null>(null)
  /** Wall-clock start of the active recording, for the countdown to the cap. */
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [stateScope, setStateScope] = useState(() => ({ sessionId, client }))
  const recorderRef = useRef<Recorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const recordingRequestedRef = useRef(false)
  const stopRequestedRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const startedAtRef = useRef<number | null>(null)
  /** Invalidates every pending permission, recorder, or upload callback. */
  const generationRef = useRef(0)
  const sessionRef = useRef(sessionId)
  const clientRef = useRef(client)

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const releaseCapture = useCallback(() => {
    clearTimer()
    recordingRequestedRef.current = false
    stopRequestedRef.current = false
    const recorder = recorderRef.current
    recorderRef.current = null
    if (recorder) {
      recorder.ondataavailable = null
      recorder.onstop = null
      recorder.onerror = null
      if (recorder.state === 'recording') recorder.stop()
    }
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    startedAtRef.current = null
  }, [clearTimer])

  const stop = useCallback(() => {
    recordingRequestedRef.current = false
    clearTimer()
    const recorder = recorderRef.current
    if (recorder?.state === 'recording') {
      stopRequestedRef.current = true
      recorder.stop()
    }
  }, [clearTimer])

  const reset = useCallback(() => {
    generationRef.current += 1
    releaseCapture()
    setStateScope({ sessionId, client })
    setStartedAt(null)
    setStatus('idle')
    setDetail(null)
    setOutcome(null)
  }, [client, releaseCapture, sessionId])

  const start = useCallback(async () => {
    // Effects own external-resource rebinding. Until the new scope has reached
    // that boundary, remain idle rather than starting against the old client.
    if (sessionRef.current !== sessionId || clientRef.current !== client) return
    if (recorderRef.current !== null || recordingRequestedRef.current) return
    const generation = ++generationRef.current
    const requestSession = sessionId
    const isCurrent = () =>
      generationRef.current === generation && sessionRef.current === requestSession
    recordingRequestedRef.current = true
    setStateScope({ sessionId: requestSession, client })
    setStatus('requesting_microphone')
    setDetail(null)
    setOutcome(null)
    let stream: MediaStream
    try {
      stream = await requestAudio()
    } catch {
      if (!isCurrent()) return
      recordingRequestedRef.current = false
      setStatus('error')
      setDetail('Microphone access was not granted. No audio was sent.')
      return
    }
    if (!isCurrent() || !recordingRequestedRef.current) {
      stream.getTracks().forEach((track) => track.stop())
      if (isCurrent()) setStatus('idle')
      return
    }
    let recorder: Recorder
    try {
      recorder = recorderFactory(stream)
    } catch {
      stream.getTracks().forEach((track) => track.stop())
      if (!isCurrent()) return
      recordingRequestedRef.current = false
      setStatus('error')
      setDetail('Recording could not start. No audio was sent.')
      return
    }
    const chunks: Blob[] = []
    const correlationId = nextId()
    stopRequestedRef.current = false
    streamRef.current = stream
    startedAtRef.current = now()
    recorder.ondataavailable = (event) => {
      if (isCurrent() && event.data.size > 0) chunks.push(event.data)
    }
    recorder.onstop = () => {
      clearTimer()
      const stopRequested = stopRequestedRef.current
      stopRequestedRef.current = false
      stream.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      recordingRequestedRef.current = false
      recorderRef.current = null
      const startedAt = startedAtRef.current
      startedAtRef.current = null
      if (!isCurrent()) return
      setStartedAt(null)
      if (!stopRequested) {
        setStatus('error')
        setDetail('Recording stopped unexpectedly. No audio was sent.')
        return
      }
      const audio = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' })
      if (audio.size === 0) {
        setStatus('error')
        setDetail('No audio was captured. Nothing was sent.')
        return
      }
      setStatus('uploading')
      const durationMs = startedAt === null ? 0 : Math.max(0, Math.min(maxRecordingMs, now() - startedAt))
      void client
        .transcribe({ sessionId: requestSession, correlationId, audio, durationMs })
        .then((received) => {
          if (!isCurrent()) return
          if (
            received.session !== requestSession ||
            received.correlation_id !== correlationId
          ) {
            setStatus('error')
            setDetail('Voice relay returned a response for another request. Nothing was emitted.')
            return
          }
          setOutcome(received)
          setStatus(received.status)
          setDetail(received.reason)
        })
        .catch((error: unknown) => {
          if (!isCurrent()) return
          setStatus('error')
          setDetail(error instanceof Error ? error.message : 'Voice upload failed. No command was sent.')
        })
    }
    const discardRecording = () => {
      clearTimer()
      recorder.ondataavailable = null
      recorder.onstop = null
      recorder.onerror = null
      recorderRef.current = null
      recordingRequestedRef.current = false
      stopRequestedRef.current = false
      startedAtRef.current = null
      if (isCurrent()) setStartedAt(null)
      stream.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    }
    recorder.onerror = () => {
      discardRecording()
      if (recorder.state === 'recording') recorder.stop()
      if (!isCurrent()) return
      setStatus('error')
      setDetail('Recording failed. No audio was sent.')
    }
    recorderRef.current = recorder
    try {
      recorder.start()
    } catch {
      discardRecording()
      if (!isCurrent()) return
      setStatus('error')
      setDetail('Recording could not start. No audio was sent.')
      return
    }
    if (!isCurrent()) {
      discardRecording()
      return
    }
    setStatus('recording')
    setStartedAt(startedAtRef.current)
    timerRef.current = setTimeout(stop, maxRecordingMs)
  }, [clearTimer, client, maxRecordingMs, nextId, now, recorderFactory, requestAudio, sessionId, stop])

  useEffect(() => {
    sessionRef.current = sessionId
    clientRef.current = client
    generationRef.current += 1
    releaseCapture()
    return () => {
      generationRef.current += 1
      releaseCapture()
    }
  }, [client, releaseCapture, sessionId])

  const scopeIsCurrent = stateScope.sessionId === sessionId && stateScope.client === client
  return {
    status: scopeIsCurrent ? status : 'idle',
    detail: scopeIsCurrent ? detail : null,
    outcome: scopeIsCurrent ? outcome : null,
    start,
    stop,
    reset,
    isRecording: scopeIsCurrent && status === 'recording',
    startedAt: scopeIsCurrent ? startedAt : null,
    maxRecordingMs,
  }
}
