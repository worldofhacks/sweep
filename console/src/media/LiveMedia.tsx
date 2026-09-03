import { useEffect, useRef, useState } from 'react'
import { createPlaybackDescriptor, type MediaRuntimeConfiguration } from './playback'
import { MediaPlaybackSession, type MediaPlaybackState } from './player'

export interface MediaSession {
  start(
    video: HTMLVideoElement,
    descriptor: ReturnType<typeof createPlaybackDescriptor>,
    onState: (state: MediaPlaybackState, detail?: string) => void,
  ): Promise<void>
  close(): Promise<void>
}

interface LiveMediaProps {
  droneId: number
  configuration: MediaRuntimeConfiguration
  createSession?: () => MediaSession
}

const createDefaultSession = () => new MediaPlaybackSession()

export function LiveMedia({
  droneId,
  configuration,
  createSession = createDefaultSession,
}: LiveMediaProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [state, setState] = useState<MediaPlaybackState>('connecting')
  const [detail, setDetail] = useState<string | undefined>()

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const session = createSession()
    const descriptor = createPlaybackDescriptor({ ...configuration, droneId })
    let active = true
    void session.start(video, descriptor, (next, nextDetail) => {
      if (active) {
        setState(next)
        if (nextDetail) setDetail(nextDetail)
      }
    }).catch(() => undefined)
    return () => {
      active = false
      void session.close()
    }
  }, [configuration, createSession, droneId])

  return (
    <div className="live-media" data-playback-state={state} data-playback-detail={detail}>
      <video
        ref={videoRef}
        className="live-video"
        controls
        muted
        playsInline
        aria-label={`Live feed for D-${String(droneId).padStart(2, '0')}`}
      />
      <span className="visually-hidden" role="status">Media playback {state}</span>
    </div>
  )
}
