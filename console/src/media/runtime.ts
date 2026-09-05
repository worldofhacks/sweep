import type { MediaRuntimeConfiguration } from './playback'
import { WhepPlaybackSession, type PlaybackSession } from './player'

/**
 * Everything a module needs to play a reported stream. Absent when the runtime
 * endpoint provided no media configuration; the Live module then says so.
 */
export interface MediaRuntime {
  configuration: MediaRuntimeConfiguration
  createSession: () => PlaybackSession
}

export function createMediaRuntime(configuration: MediaRuntimeConfiguration): MediaRuntime {
  return { configuration, createSession: () => new WhepPlaybackSession() }
}
