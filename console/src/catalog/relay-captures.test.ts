import { describe, expect, test } from 'vitest'
import type { RelayCaptureFile, RelayCaptureRecord } from '../relay/contract'
import { closedRelayCaptures, openRelayCaptures } from './relay-captures'

const t = 1_756_700_000_000

function file(overrides: Partial<RelayCaptureFile> = {}): RelayCaptureFile {
  return {
    capture_id: 'cap-1',
    file_id: 'cap-1-frame-01',
    timestamp_ms: t,
    drone_id: 1,
    connection_epoch: 1,
    pose: { x: 1.5, y: -0.25, z: 1.2 },
    actual_yaw_deg: 45,
    gimbal_pitch_deg: -15,
    intrinsics: { width_px: 4000, height_px: 3000, horizontal_fov_deg: 82.1, projection: 'rectilinear' },
    checksum_sha256: 'a'.repeat(64),
    storage_ref: 'file:///data/user/0/org.worldofhacks.sweep.bridge/files/captures/cap-1/DJI_0001.JPG',
    retrieval_status: 'completed',
    ...overrides,
  }
}

function capture(overrides: Partial<RelayCaptureRecord> = {}): RelayCaptureRecord {
  return {
    capture_id: 'cap-1',
    drone_id: 1,
    connection_epoch: 1,
    room_id: null,
    pattern: null,
    coverage: null,
    status: null,
    reason: null,
    detail: null,
    files: [file()],
    updated_at: t + 5,
    ...overrides,
  }
}

describe('relay captures', () => {
  test('a closed capture becomes a catalog record under the session as its project', () => {
    const [record] = closedRelayCaptures(
      [
        capture({
          room_id: 'room-1',
          pattern: 'reconstruct_8',
          coverage: 'incomplete_vertical_coverage',
          status: 'completed',
          files: [file({ file_id: 'cap-1-frame-02', timestamp_ms: t + 2, actual_yaw_deg: 90 }), file()],
        }),
      ],
      'demo',
    )
    expect(record).toEqual({
      capture_id: 'cap-1',
      project: 'demo',
      room_id: 'room-1',
      drone_id: 1,
      pattern: 'reconstruct_8',
      coverage: 'incomplete_vertical_coverage',
      files: 2,
      captured_at: t,
      quality: 'pass',
      needs_retake: false,
      checksum: null,
      pose: { x: 1.5, y: -0.25, z: 1.2, yaw_deg: 45, gimbal_pitch_deg: -15, focal_mm: null },
    })
  })

  test('one retrieved file carries its checksum; a failed bundle needs a retake', () => {
    const [record] = closedRelayCaptures(
      [
        capture({
          room_id: 'room-1',
          pattern: 'pano_360',
          coverage: 'full_equirectangular',
          status: 'failed',
          reason: 'camera_failure',
        }),
      ],
      'demo',
    )
    expect(record.checksum).toBe(`sha256:${'a'.repeat(64)}`)
    expect(record.quality).toBe('fail')
    expect(record.needs_retake).toBe(true)
  })

  test('an open capture is progress, never a record, and a pending file counts as captured only', () => {
    const open = capture({
      files: [
        file({ retrieval_status: 'pending', checksum_sha256: '0'.repeat(64) }),
        file({ file_id: 'cap-1-frame-02', timestamp_ms: t + 1 }),
      ],
    })
    expect(closedRelayCaptures([open], 'demo')).toEqual([])
    expect(openRelayCaptures([open])).toEqual([
      {
        capture_id: 'cap-1',
        drone_id: 1,
        connection_epoch: 1,
        files: 2,
        retrieved: 1,
        phase: 'downloading',
        updated_at: t + 5,
      },
    ])
    expect(openRelayCaptures([capture({ files: [file({ retrieval_status: 'pending', checksum_sha256: '0'.repeat(64) })] })])[0].phase).toBe(
      'capturing',
    )
  })
})
