import type { RelayCaptureFile, RelayCaptureRecord } from '../relay/contract'
import type { CaptureRecord } from './types'

/**
 * The relay's `state.captures` projection as the Captures module lists it. A capture the
 * relay closed with a bundle becomes a catalog record (the session id is its project; a
 * failed or unsupported bundle needs a retake); a capture with files but no bundle is
 * progress, not a record, because the relay reports no room, pattern, or quality for it.
 * Nothing here invents a field the relay did not send.
 */

export interface OpenCapture {
  capture_id: string
  drone_id: number
  connection_epoch: number
  files: number
  retrieved: number
  /** `capturing` until a file is retrieved, then `downloading`. */
  phase: 'capturing' | 'downloading'
  updated_at: number
}

export function closedRelayCaptures(captures: RelayCaptureRecord[], sessionId: string): CaptureRecord[] {
  return captures.flatMap((capture) => {
    if (capture.status === null || capture.pattern === null || capture.coverage === null) return []
    const first = earliestFile(capture.files)
    const completed = capture.files.filter((file) => file.retrieval_status === 'completed')
    return [
      {
        capture_id: capture.capture_id,
        project: sessionId,
        room_id: capture.room_id ?? 'unreported',
        drone_id: capture.drone_id,
        pattern: capture.pattern,
        coverage: capture.coverage,
        files: capture.files.length,
        captured_at: first?.timestamp_ms ?? capture.updated_at,
        quality: capture.status === 'completed' ? 'pass' : 'fail',
        needs_retake: capture.status !== 'completed',
        // One retrieved file has one checksum; a multi-file set has one per file, listed in the export.
        checksum: completed.length === 1 ? `sha256:${completed[0].checksum_sha256}` : null,
        pose:
          first === null
            ? null
            : {
                x: first.pose.x,
                y: first.pose.y,
                z: first.pose.z,
                yaw_deg: first.actual_yaw_deg,
                gimbal_pitch_deg: first.gimbal_pitch_deg,
                focal_mm: null,
              },
      },
    ]
  })
}

export function openRelayCaptures(captures: RelayCaptureRecord[]): OpenCapture[] {
  return captures
    .filter((capture) => capture.status === null)
    .map((capture): OpenCapture => {
      const retrieved = capture.files.filter((file) => file.retrieval_status === 'completed').length
      return {
        capture_id: capture.capture_id,
        drone_id: capture.drone_id,
        connection_epoch: capture.connection_epoch,
        files: capture.files.length,
        retrieved,
        phase: retrieved > 0 ? 'downloading' : 'capturing',
        updated_at: capture.updated_at,
      }
    })
    .sort((a, b) => b.updated_at - a.updated_at)
}

function earliestFile(files: RelayCaptureFile[]): RelayCaptureFile | null {
  return files.reduce<RelayCaptureFile | null>(
    (earliest, file) => (earliest === null || file.timestamp_ms < earliest.timestamp_ms ? file : earliest),
    null,
  )
}
