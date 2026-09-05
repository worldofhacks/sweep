/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/LiveMedia.tsx)
 * and restyled for the Live module's focus feed. Reconcile when #68 merges.
 */
import { useMemo, useRef } from 'react'
import { formatDroneId } from '../control/state'
import type { DroneId } from '../relay/contract'
import { createPlaybackDescriptor, streamName, type PlaybackDescriptor } from './playback'
import type { MediaRuntime } from './runtime'
import { usePlayback } from './use-playback'

export interface LivePlayerProps {
  droneId: DroneId
  media: MediaRuntime
}

/** Mounted only while the relay reports the stream live; unmounting closes the session. */
export function LivePlayer({ droneId, media }: LivePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const descriptor = useMemo<PlaybackDescriptor | Error>(() => {
    try {
      return createPlaybackDescriptor({ ...media.configuration, droneId })
    } catch (error) {
      return error instanceof Error ? error : new Error('No playback descriptor')
    }
  }, [droneId, media.configuration])
  const playback = usePlayback(
    videoRef,
    descriptor instanceof Error ? null : descriptor,
    media.createSession,
  )
  const caption = describePlayback(droneId, playback.state, playback.detail, descriptor)

  return (
    <div className="lv-player" data-playback-state={playback.state}>
      <video ref={videoRef} muted playsInline aria-label={`Live feed ${formatDroneId(droneId)}`} />
      <p className="visually-hidden" role="status">
        Playback {playback.state}
      </p>
      {caption && (
        <p className={caption.failed ? 'lv-playback is-failed' : 'lv-playback'}>{caption.text}</p>
      )}
    </div>
  )
}

function describePlayback(
  droneId: DroneId,
  state: ReturnType<typeof usePlayback>['state'],
  detail: string | undefined,
  descriptor: PlaybackDescriptor | Error,
): { text: string; failed: boolean } | null {
  if (descriptor instanceof Error) {
    return { text: `No playback path for ${streamName(droneId)}: ${descriptor.message}.`, failed: true }
  }
  if (state === 'playing') return null
  if (state === 'failed') {
    return {
      text: `Playback failed: ${detail ?? 'no detail'}. The relay still reports the stream live.`,
      failed: true,
    }
  }
  return { text: `Connecting to ${descriptor.stream} over WHEP.`, failed: false }
}
