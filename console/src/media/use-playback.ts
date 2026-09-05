/**
 * The playback effect from PR #68's LiveMedia component
 * (feat/m31-media-ingest, console/src/media/LiveMedia.tsx), lifted into a
 * hook so the Live module's focus feed can host the video element itself.
 */
import { useEffect, useState, type RefObject } from 'react'
import type { PlaybackDescriptor } from './playback'
import type { MediaPlaybackState, PlaybackSession } from './player'

export interface PlaybackView {
  state: 'idle' | MediaPlaybackState
  detail?: string
}

export function usePlayback(
  videoRef: RefObject<HTMLVideoElement | null>,
  descriptor: PlaybackDescriptor | null,
  createSession: () => PlaybackSession,
): PlaybackView {
  const [view, setView] = useState<PlaybackView>({ state: 'idle' })

  useEffect(() => {
    const video = videoRef.current
    if (!video || !descriptor) return
    const session = createSession()
    let active = true
    void session.start(video, descriptor, (state, detail) => {
      if (active) setView({ state, detail })
    })
    return () => {
      active = false
      void session.close()
    }
  }, [createSession, descriptor, videoRef])

  return view
}
