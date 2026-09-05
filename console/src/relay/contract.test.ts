import { describe, expect, test } from 'vitest'
import { isConsoleIntentV1, parseRelayServerEvent } from './contract'

const session = 'session-contract-test'

function aircraft(overrides: Record<string, unknown> = {}) {
  return {
    drone_id: 1,
    connection_epoch: 2,
    membership: 'ready',
    readiness_reasons: [],
    flight_state: 'hovering',
    battery: 0.81,
    link: 0.93,
    pos_quality: 0.9,
    control_authority: true,
    last_seen_at: 1_756_700_000_000,
    camera_patterns: ['pano_360', 'reconstruct_8'],
    selectable: true,
    adapter_id: 'mini-3-bridge-1',
    adapter_capabilities: ['flight', 'camera:pano_360'],
    home_pose: { x: 0, y: 0, z: 0 },
    rc_safety_operator_present: true,
    telemetry: { state: 'hovering' },
    membership_history: [],
    ...overrides,
  }
}

describe('M1.1 wire compatibility', () => {
  test('refuses an adapter-supplied media URL', () => {
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_000,
        type: 'state',
        event_id: 'state-media-url',
        session,
        roster_version: 4,
        armed: true,
        estop: false,
        selection: [1],
        formation: 'line',
        spacing: 0.8,
        mode: 'indoor',
        pending: null,
        accepted_plan: null,
        drones: [
          aircraft({
            video: {
              status: 'live',
              last_frame_at: 1_756_700_000_000,
              url: 'https://adapter.example.invalid/stream',
            },
          }),
        ],
      }),
    ).toBeNull()
  })

  test('accepts the exact authoritative state projection with forward-compatible fields', () => {
    const event = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_000,
      type: 'state',
      event_id: 'state-1',
      session,
      roster_version: 4,
      armed: true,
      estop: false,
      selection: [1],
      formation: 'line',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      drones: [aircraft()],
    })

    expect(event).not.toBeNull()
    expect(event?.type).toBe('state')
  })

  test('accepts the one-shot graceful-leave invalidation state fields', () => {
    const event = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_005,
      type: 'state',
      event_id: 'state-leave',
      session,
      roster_version: 5,
      prior_roster_version: 4,
      armed: true,
      estop: false,
      selection: [],
      formation: 'none',
      spacing: 0.8,
      mode: 'indoor',
      pending: null,
      accepted_plan: null,
      invalidated_intent_ids: ['intent-capture'],
      invalidation_reason: 'graceful_leave_roster_change',
      cleared_control_fields: ['selection', 'pending', 'accepted_plan'],
      drones: [aircraft({ membership: 'leaving', selectable: false })],
    })

    expect(event).toMatchObject({
      type: 'state',
      prior_roster_version: 4,
      invalidation_reason: 'graceful_leave_roster_change',
      cleared_control_fields: ['selection', 'pending', 'accepted_plan'],
    })
  })

  test('accepts flat membership transitions and retains signed provenance', () => {
    const event = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_010,
      type: 'membership',
      event_id: 'membership-1',
      session,
      action: 'telemetry_stale',
      drone_id: 1,
      connection_epoch: 2,
      membership: 'degraded',
      roster_version: 4,
      reason: 'telemetry_stale',
      readiness_reasons: ['telemetry_stale'],
      adapter_id: 'mini-3-bridge-1',
      capabilities: ['flight', 'camera:pano_360'],
      provenance: 'relay_freshness_attestation',
    })

    expect(event).toMatchObject({
      type: 'membership',
      drone_id: 1,
      provenance: 'relay_freshness_attestation',
    })
  })

  test('accepts the exact Appendix B telemetry event emitted before state', () => {
    const event = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_015,
      type: 'telemetry',
      event_id: 'telemetry-1',
      session,
      drone: 1,
      connection_epoch: 2,
      x: 1,
      y: 2,
      z: 0.5,
      vx: 0,
      vy: 0,
      vz: 0,
      battery: 0.8,
      state: 'hovering',
      link: 0.9,
      pos_quality: 0.95,
    })

    expect(event).toMatchObject({
      type: 'telemetry',
      drone: 1,
      connection_epoch: 2,
    })
  })

  test('accepts intent-level and command-scoped acknowledgement variants', () => {
    const accepted = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_020,
      type: 'acknowledgement',
      event_id: 'ack-accepted',
      session,
      intent_id: 'intent-1',
      command_id: null,
      status: 'accepted',
      source: 'relay',
      drone_id: null,
      connection_epoch: null,
      roster_version: 4,
      reason: null,
      detail: null,
    })
    const failedCommand = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_021,
      type: 'acknowledgement',
      event_id: 'ack-failed',
      session,
      intent_id: 'intent-1',
      command_id: 'command-2',
      status: 'failed',
      source: 'adapter',
      drone_id: 1,
      connection_epoch: 2,
      roster_version: 4,
      reason: 'adapter_timeout',
      detail: 'Adapter did not acknowledge before the deadline.',
    })

    expect(accepted).toMatchObject({ type: 'acknowledgement', command_id: null })
    expect(failedCommand).toMatchObject({ status: 'failed', command_id: 'command-2' })
  })

  test('rejects empty command IDs while retaining nullable intent-level context', () => {
    const acknowledgement = {
      v: 1,
      t: 1_756_700_000_022,
      type: 'acknowledgement',
      event_id: 'ack-empty-command',
      session,
      intent_id: 'intent-1',
      command_id: '',
      status: 'accepted',
      source: 'relay',
      drone_id: null,
      connection_epoch: null,
      roster_version: 4,
      reason: null,
      detail: null,
    }
    const refusal = {
      ...acknowledgement,
      type: 'refusal',
      event_id: 'refusal-empty-command',
      status: 'refused',
      reason: 'invalid_command_id',
      detail: 'Command IDs must not be empty.',
    }

    expect(parseRelayServerEvent(acknowledgement)).toBeNull()
    expect(parseRelayServerEvent(refusal)).toBeNull()
    expect(parseRelayServerEvent({ ...acknowledgement, command_id: null })).not.toBeNull()
  })

  test('accepts auth success, auth refusal, and a protocol refusal without intent correlation', () => {
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_030,
        type: 'auth.accepted',
        event_id: 'auth-1',
        session,
        source: 'console',
        drone_id: null,
      }),
    ).toMatchObject({ type: 'auth.accepted' })
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_030,
        type: 'auth.accepted',
        event_id: 'auth-adapter',
        session,
        source: 'adapter',
        drone_id: 1,
      }),
    ).toMatchObject({ type: 'auth.accepted', source: 'adapter', drone_id: 1 })
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_031,
        type: 'auth.refused',
        event_id: 'auth-2',
        session,
        status: 'refused',
        reason: 'invalid_token',
        detail: 'Authentication failed.',
      }),
    ).toMatchObject({ type: 'auth.refused', reason: 'invalid_token' })
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_032,
        type: 'refusal',
        event_id: 'refusal-1',
        session,
        intent_id: null,
        command_id: null,
        status: 'refused',
        source: 'relay',
        drone_id: null,
        connection_epoch: null,
        roster_version: 4,
        reason: 'invalid_payload',
        detail: 'Intent frame was malformed.',
      }),
    ).toMatchObject({ type: 'refusal', intent_id: null })
  })
})

describe('console Intent v1 mirror', () => {
  test.each([
    ['console', 'hold', {}, [1], false],
    ['console', 'estop', {}, [], false],
    ['keyboard', 'estop', {}, [], false],
    ['console', 'select', { ids: [1, 2] }, [1], false],
    ['console', 'arm', {}, [1], false],
    ['console', 'disarm', {}, [], false],
    ['console', 'takeoff', {}, [1, 2], true],
    ['console', 'land', {}, [1], true],
    ['console', 'land_all', {}, [1, 2, 3, 4], true],
    ['console', 'translate', { dx: -2, dy: 0 }, [1], false],
    ['console', 'altitude', { delta: 1 }, [1], false],
    ['console', 'formation_next', {}, [1], false],
    ['console', 'formation_set', { name: 'V' }, [1, 2], false],
    ['console', 'spacing', { delta: -1 }, [1], false],
    ['console', 'come_home', {}, [1], false],
    ['console', 'sweep', {}, [1], true],
    ['console', 'sweep', { box: { x: 0, y: 0, w: 2, h: 2 } }, [1], true],
    [
      'console',
      'capture_room',
      { room_id: 'room-1', capture_id: 'capture-1', pattern: 'pano_360' },
      [1],
      true,
    ],
  ])('accepts %s %s payloads produced by the control boundary', (source, name, args, selection, confirm) => {
    expect(
      isConsoleIntentV1({
        v: 1,
        t: 1_756_700_000_000,
        type: 'intent',
        intent_id: 'intent-1',
        retry_of: null,
        source,
        session,
        name,
        args,
        selection,
        mode: 'indoor',
        confirm,
      }),
    ).toBe(true)
  })
})
