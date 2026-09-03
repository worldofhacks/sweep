import { useCallback, useEffect, useRef, useState } from 'react'
import type { TranscriptClient, VoiceOutcome } from './client'

export const MAX_RECORDING_MS = 30_000

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
}

export function usePushToTalk({
  sessionId,
  client,
  requestAudio = () => navigator.mediaDevices.getUserMedia({ audio: true }),
  recorderFactory = (stream) => new MediaRecorder(stream),
  nextId = () => crypto.randomUUID(),
  maxRecordingMs = MAX_RECORDING_MS,
}: UsePushToTalkOptions) {
  const [status, setStatus] = useState<PushToTalkStatus>('idle')
  const [detail, setDetail] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<VoiceOutcome | null>(null)
  const recorderRef = useRef<Recorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const recordingRequestedRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const startedAtRef = useRef<number | null>(null)

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const stop = useCallback(() => {
    recordingRequestedRef.current = false
    clearTimer()
    const recorder = recorderRef.current
    if (recorder?.state === 'recording') recorder.stop()
  }, [clearTimer])

  const start = useCallback(async () => {
    if (recorderRef.current !== null || recordingRequestedRef.current) return
    recordingRequestedRef.current = true
    setStatus('requesting_microphone')
    setDetail(null)
    setOutcome(null)
    let stream: MediaStream
    try {
      stream = await requestAudio()
    } catch {
      recordingRequestedRef.current = false
      setStatus('error')
      setDetail('Microphone access was not granted. No audio was sent.')
      return
    }
    if (!recordingRequestedRef.current) {
      stream.getTracks().forEach((track) => track.stop())
      setStatus('idle')
      return
    }
    let recorder: Recorder
    try {
      recorder = recorderFactory(stream)
    } catch {
      stream.getTracks().forEach((track) => track.stop())
      recordingRequestedRef.current = false
      setStatus('error')
      setDetail('Recording could not start. No audio was sent.')
      return
    }
    const chunks: Blob[] = []
    const correlationId = nextId()
    streamRef.current = stream
    startedAtRef.current = Date.now()
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data)
    }
    recorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      recordingRequestedRef.current = false
      recorderRef.current = null
      const startedAt = startedAtRef.current
      startedAtRef.current = null
      const audio = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' })
      if (audio.size === 0) {
        setStatus('error')
        setDetail('No audio was captured. Nothing was sent.')
        return
      }
      setStatus('uploading')
      const durationMs = startedAt === null ? 0 : Math.max(0, Math.min(maxRecordingMs, Date.now() - startedAt))
      void client
        .transcribe({ sessionId, correlationId, audio, durationMs })
        .then((received) => {
          setOutcome(received)
          setStatus(received.status)
          setDetail(received.reason)
        })
        .catch((error: unknown) => {
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
      startedAtRef.current = null
      stream.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    }
    recorder.onerror = () => {
      discardRecording()
      if (recorder.state === 'recording') recorder.stop()
      setStatus('error')
      setDetail('Recording failed. No audio was sent.')
    }
    recorderRef.current = recorder
    try {
      recorder.start()
    } catch {
      discardRecording()
      setStatus('error')
      setDetail('Recording could not start. No audio was sent.')
      return
    }
    setStatus('recording')
    timerRef.current = setTimeout(stop, maxRecordingMs)
  }, [clearTimer, client, maxRecordingMs, nextId, recorderFactory, requestAudio, sessionId, stop])

  useEffect(
    () => () => {
      clearTimer()
      recordingRequestedRef.current = false
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
    },
    [clearTimer],
  )

  return { status, detail, outcome, start, stop, isRecording: status === 'recording' }
}
