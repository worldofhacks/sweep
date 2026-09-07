import { useEffect, useState, type RefObject } from 'react'
import type { PlaybackDescriptor } from './playback'
import type { MediaPlaybackState, PlaybackSession } from './player'

export interface PlaybackView {
  state: 'idle' | 'reconnecting' | MediaPlaybackState
  detail?: string
  phase?: string
  retryDelayMs?: number
}

export const RETRY_BASE_MS = 1_000
export const RETRY_MAX_MS = 8_000
const STABLE_PLAYBACK_MS = 10_000

export function usePlayback(
  videoRef: RefObject<HTMLVideoElement | null>,
  descriptor: PlaybackDescriptor | null,
  createSession: () => PlaybackSession,
): PlaybackView {
  const [view, setView] = useState<PlaybackView>({ state: 'idle' })

  useEffect(() => {
    const video = videoRef.current
    if (!video || !descriptor) return
    let active = true
    let generation = 0
    let session: PlaybackSession | null = null
    let retryTimer: number | undefined
    let failures = 0
    let playingSince: number | undefined
    let lastFailure: string | undefined

    const start = () => {
      const attempt = ++generation
      const failed = (detail = 'WHEP playback failed') => {
        if (!active || attempt !== generation) return
        // Invalidate all callbacks from this attempt before asynchronous cleanup.
        const retryGeneration = ++generation
        if (playingSince !== undefined && Date.now() - playingSince >= STABLE_PLAYBACK_MS) failures = 0
        playingSince = undefined
        lastFailure = detail
        const delay = Math.min(RETRY_BASE_MS * 2 ** Math.min(failures++, 3), RETRY_MAX_MS)
        setView({ state: 'reconnecting', detail })
        const failedSession = session
        session = null
        void closeSession(failedSession).then(() => {
          if (!active || retryGeneration !== generation) return
          setView({ state: 'reconnecting', detail, retryDelayMs: delay })
          retryTimer = window.setTimeout(() => {
            retryTimer = undefined
            start()
          }, delay)
        })
      }
      try {
        const nextSession = createSession()
        session = nextSession
        void Promise.resolve().then(() => {
          if (!active || attempt !== generation) return
          return nextSession.start(video, descriptor, (state, detail) => {
            if (!active || attempt !== generation) return
            if (state === 'failed') { failed(detail); return }
            if (state === 'playing') {
              playingSince = Date.now()
              lastFailure = undefined
              setView({ state })
            } else {
              playingSince = undefined
              setView(lastFailure
                ? { state: 'reconnecting', detail: lastFailure, phase: detail }
                : { state, detail })
            }
          })
        }).catch((error: unknown) => failed(error instanceof Error ? error.message : undefined))
      } catch (error) {
        failed(error instanceof Error ? error.message : undefined)
      }
    }
    start()
    return () => {
      active = false
      generation += 1
      window.clearTimeout(retryTimer)
      const closingSession = session
      session = null
      void closeSession(closingSession)
    }
  }, [createSession, descriptor, videoRef])

  return descriptor ? view : { state: 'idle' }
}

function closeSession(session: PlaybackSession | null): Promise<void> {
  try {
    return session?.close().catch(() => undefined) ?? Promise.resolve()
  } catch {
    return Promise.resolve()
  }
}
