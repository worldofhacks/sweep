import { describe, expect, test } from 'vitest'
import type { RequestRecord } from '../../control/state'
import type { IntentV1, RelayAircraftState } from '../../relay/contract'
import { fixtureAircraft } from '../../testing/fixture-relay-client'
import {
  DEGRADED_WORD,
  deriveCaptureProgress,
  deriveReadiness,
  deriveStream,
  formatAge,
} from './derive-live'

const now = 1_756_700_000_000

function drone(overrides: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return { ...fixtureAircraft(now)[0], ...overrides }
}

function captureRequest(
  status: RequestRecord['status'],
  selection: number[] = [1],
  name: IntentV1['name'] = 'capture_room',
): RequestRecord {
  return {
    intent: {
      v: 1,
      t: now,
      type: 'intent',
      intent_id: `${name}-${status}`,
      retry_of: null,
      source: 'console',
      session: 'derive-live',
      name,
      args: { room_id: 'room-01', capture_id: 'capture-1', pattern: 'pano_360' },
      selection,
      mode: 'indoor',
      confirm: true,
    },
    status,
    timestamps: { [status]: now },
  }
}

describe('frame age', () => {
  test('says just now under half a second and whole seconds otherwise, as the design', () => {
    expect(formatAge(0)).toBe('just now')
    expect(formatAge(499)).toBe('just now')
    expect(formatAge(500)).toBe('1 s ago')
    expect(formatAge(38_000)).toBe('38 s ago')
  })

  test('never reports a negative age when clocks disagree', () => {
    expect(formatAge(-4_000)).toBe('just now')
  })
})

describe('stream view', () => {
  test('a live stream carries the ok tone, its age, and no degraded word', () => {
    expect(deriveStream(drone({ video: { status: 'live', last_frame_at: now - 400 } }), now)).toEqual({
      status: 'live',
      tone: 'ok',
      lastFrame: 'just now',
      degraded: false,
      degradedWord: '',
    })
  })

  test('offline and unreported streams say so in the design words', () => {
    expect(
      deriveStream(drone({ video: { status: 'offline', last_frame_at: now - 12_000 } }), now),
    ).toMatchObject({
      status: 'offline',
      tone: 'warn',
      lastFrame: '12 s ago',
      degraded: true,
      degradedWord: DEGRADED_WORD.offline,
    })
    expect(
      deriveStream(drone({ video: { status: 'unreported', last_frame_at: null } }), now),
    ).toMatchObject({
      status: 'unreported',
      tone: 'muted',
      lastFrame: 'no frame reported',
      degraded: true,
      degradedWord: DEGRADED_WORD.unreported,
    })
  })

  test('an aircraft without a video field is unreported rather than invented', () => {
    expect(deriveStream(drone({ video: undefined }), now)).toMatchObject({
      status: 'unreported',
      lastFrame: 'no frame reported',
      degraded: true,
    })
  })
})

describe('readiness word', () => {
  test('joins the relay reasons or says ready', () => {
    expect(deriveReadiness(drone())).toEqual({ text: 'ready', tone: 'ok' })
    expect(deriveReadiness(drone({ readiness_reasons: ['telemetry_stale', 'home_pose_missing'] }))).toEqual({
      text: 'Telemetry stale, Home pose missing',
      tone: 'danger',
    })
  })

  test('missing authority says control is not granted without inventing an interlock cause', () => {
    expect(deriveReadiness(drone({ readiness_reasons: ['control_authority_missing'] }))).toEqual({
      text: 'Control not granted', tone: 'danger',
    })
  })
})

describe('capture progress', () => {
  test('is none requested until a capture_room request targets the aircraft', () => {
    expect(deriveCaptureProgress([], 1)).toEqual({ text: 'none requested', tone: 'muted' })
    expect(deriveCaptureProgress([captureRequest('accepted', [2])], 1)).toEqual({
      text: 'none requested',
      tone: 'muted',
    })
    expect(deriveCaptureProgress([captureRequest('accepted', [1], 'hold')], 1)).toEqual({
      text: 'none requested',
      tone: 'muted',
    })
  })

  test('ignores drafts and reads the newest request first', () => {
    expect(deriveCaptureProgress([captureRequest('draft')], 1)).toEqual({
      text: 'none requested',
      tone: 'muted',
    })
    expect(
      deriveCaptureProgress([captureRequest('executing'), captureRequest('completed')], 1),
    ).toEqual({ text: 'executing', tone: 'ink' })
  })

  test('tones follow the request lifecycle', () => {
    expect(deriveCaptureProgress([captureRequest('pending_confirmation')], 1)).toEqual({
      text: 'pending confirmation',
      tone: 'ink',
    })
    expect(deriveCaptureProgress([captureRequest('completed')], 1)).toEqual({ text: 'completed', tone: 'ok' })
    expect(deriveCaptureProgress([captureRequest('failed')], 1)).toEqual({ text: 'failed', tone: 'danger' })
    expect(deriveCaptureProgress([captureRequest('refused')], 1)).toEqual({ text: 'refused', tone: 'danger' })
    expect(deriveCaptureProgress([captureRequest('invalidated')], 1)).toEqual({
      text: 'invalidated',
      tone: 'warn',
    })
    expect(deriveCaptureProgress([captureRequest('cancelled')], 1)).toEqual({ text: 'cancelled', tone: 'warn' })
  })
})
