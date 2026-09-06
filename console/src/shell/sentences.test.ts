import { describe, expect, test } from 'vitest'
import {
  INVALIDATION,
  MEMBERSHIP_REASON,
  READINESS,
  REASONS,
  membershipReasonSentence,
  readinessSentence,
  reasonSentence,
} from './sentences'

describe('sentences per device class', () => {
  test('the exported tables keep the aircraft wording', () => {
    expect(REASONS.aircraft_not_ready).toBe(
      'The aircraft has open readiness reasons and cannot accept flight commands.',
    )
    expect(READINESS.rc_safety_operator_missing).toBe('No RC safety operator is reported present.')
    expect(MEMBERSHIP_REASON.graceful_leave_requested).toBe('The aircraft asked to leave.')
    expect(INVALIDATION.aircraft_departed).toBe('An aircraft in the plan left the session.')
    expect(Object.values(REASONS).every((sentence) => typeof sentence === 'string')).toBe(true)
  })

  test('a robot gets the robot wording, and a mixed set says device', () => {
    expect(reasonSentence('aircraft_not_ready', 'robot')).toBe(
      'The robot has open readiness reasons and cannot accept motion commands.',
    )
    expect(reasonSentence('unsupported_for_device_class', 'robot')).toContain('robots')
    expect(reasonSentence('rc_safety_operator_absent', 'robot')).toContain('spotter')
    expect(reasonSentence('spacing', 'device')).toBe(
      'The commanded formation would put two devices closer than the spacing limit.',
    )
    expect(readinessSentence('rc_safety_operator_missing', 'robot')).toBe(
      'No spotter is reported present beside the robot with its screen stop in reach.',
    )
    expect(readinessSentence('drive_capability_missing', 'robot')).toBe(
      'The adapter does not advertise ground drive control.',
    )
    expect(readinessSentence('telemetry_missing', 'device')).toBe('No telemetry has arrived for this device.')
    expect(membershipReasonSentence('device_class_mismatch', 'robot')).toContain('device class')
    expect(reasonSentence('aircraft_departed', 'robot')).toBe('A robot in the plan left the session.')
    expect(reasonSentence(undefined, 'robot')).toBe('')
    expect(readinessSentence('unknown_code', 'robot')).toBeUndefined()
  })
})
