/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/LiveMedia.tsx)
 * and restyled for the Live module's focus feed. Reconcile when #68 merges.
 */
import { useMemo, useRef } from 'react'
import { formatDeviceId } from '../control/state'
import type { DeviceCameraState, RelayAircraftState } from '../relay/contract'
import { createPlaybackDescriptor, streamName, type PlaybackDescriptor } from './playback'
import type { MediaRuntime } from './runtime'
import { usePlayback } from './use-playback'

export type LivePlayerDevice = Pick<RelayAircraftState, 'drone_id' | 'device_class' | 'unit'>

export interface LivePlayerProps {
  device: LivePlayerDevice
  media: MediaRuntime
  camera?: Pick<DeviceCameraState, 'camera_id' | 'label' | 'stream'>
  /** Keep decoded playback evidence visible beside camera-wall source status. */
  showPlaybackStatus?: boolean
}

/** Mounted only while the relay reports the stream live; unmounting closes the session. */
export function LivePlayer({ device, media, camera, showPlaybackStatus = false }: LivePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const { device_class, unit } = device
  const cameraStream = camera?.stream
  const descriptor = useMemo<PlaybackDescriptor | Error>(() => {
    try {
      return createPlaybackDescriptor({ ...media.configuration, device: { device_class, unit }, stream: cameraStream })
    } catch (error) {
      return error instanceof Error ? error : new Error('No playback descriptor')
    }
  }, [device_class, unit, media.configuration, cameraStream])
  const playback = usePlayback(
    videoRef,
    descriptor instanceof Error ? null : descriptor,
    media.createSession,
  )
  const caption = describePlayback(device, playback.state, playback.detail, descriptor)

  return (
    <div className="lv-player" data-playback-state={playback.state}>
      <video ref={videoRef} muted playsInline aria-label={`Live feed ${formatDeviceId(device)}${camera ? ` · ${camera.label}` : ''}`} />
      <p className="visually-hidden" role="status">
        Playback {playback.state}
      </p>
      {showPlaybackStatus && playback.state === 'playing' && (
        <p className="lv-playback is-playing">Playback · receiving frames</p>
      )}
      {caption && (
        <p className={caption.failed ? 'lv-playback is-failed' : 'lv-playback'}>
          <span className={caption.failed ? 'is-failed' : undefined}>{caption.text}</span>
          {playback.state === 'reconnecting' && (
            <>
              <br />
              <span>
                {playback.retryDelayMs === undefined
                  ? playback.phase ?? 'Reconnecting to the live feed…'
                  : `Reconnecting in ${playback.retryDelayMs / 1_000}s…`}
              </span>
            </>
          )}
        </p>
      )}
    </div>
  )
}

function describePlayback(
  device: LivePlayerDevice,
  state: ReturnType<typeof usePlayback>['state'],
  detail: string | undefined,
  descriptor: PlaybackDescriptor | Error,
): { text: string; failed: boolean } | null {
  if (descriptor instanceof Error) {
    return { text: `No playback path for ${streamName(device)}: ${descriptor.message}.`, failed: true }
  }
  if (state === 'playing') return null
  if (state === 'failed' || state === 'reconnecting') {
    return {
      text: `Playback failed: ${detail ?? 'no detail'}. The relay still reports the stream live.`,
      failed: true,
    }
  }
  return {
    text: detail ? `${detail} (${descriptor.stream}).` : `Connecting to ${descriptor.stream} over WHEP.`,
    failed: false,
  }
}
