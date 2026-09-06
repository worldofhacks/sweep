import { describe, expect, test } from 'vitest'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1, parseRelayServerEvent } from './contract'
import { REJOIN_NODE_EVENTS } from './rejoin-node-events.test-fixtures'

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
  test.each(REJOIN_NODE_EVENTS)('accepts the emitted epoch-2 $type report', (event) => {
    expect(parseRelayServerEvent(event)).toEqual(event)
  })

  test.each([
    [0, 'gimbal_pitch_min_deg', 30],
    [0, 'horizontal_fov_deg', 0],
    [0, 'horizontal_fov_deg', 361],
    [0, 'measured_hfov_deg', 180],
    [0, 'storage_remaining_bytes', -1],
    [0, 'photo_capture', 'true'],
    [0, 'native_panorama_modes', ['pano_360', 'pano_360']],
    [0, 'aircraft_model', ''],
    [1, 'phone_battery_percent', 101],
    [1, 'phone_battery_percent', 81.5],
    [1, 'control_authority', 'true'],
    [1, 'watchdog_state', 'unknown'],
    [1, 'watchdog_state', ['nominal']],
    [1, 'video_publish_state', 'http://adapter.invalid/video'],
    [1, 'phone_thermal_state', 'unknown'],
    [1, 'authority_change_reason', 'not a machine code'],
    [2, 'camera_ok', 1],
    [2, 'coverage_missing', [360]],
    [2, 'next_heading_deg', -1],
    [2, 'pose_source', ''],
    [2, 'guidance_mode', ['visual_advisory']],
    [2, 'suggested_delta', { kind: 'translate', degrees: 10 }],
    [2, 'suggested_delta', { kind: 'yaw', degrees: 10, extra: true }],
  ])('rejects malformed node report %s field %s', (index, field, value) => {
    expect(parseRelayServerEvent({ ...REJOIN_NODE_EVENTS[Number(index)], [String(field)]: value })).toBeNull()
  })

  test.each(REJOIN_NODE_EVENTS)('keeps the $type report envelope closed and epoch-bound', (event) => {
    const { connection_epoch: epoch, ...missingEpoch } = event
    expect(epoch).toBe(2)
    expect(parseRelayServerEvent(missingEpoch)).toBeNull()
    expect(parseRelayServerEvent({ ...event, connection_epoch: 0 })).toBeNull()
    expect(parseRelayServerEvent({ ...event, drone_id: -1 })).toBeNull()
    expect(parseRelayServerEvent({ ...event, event_id: 'x'.repeat(513) })).toBeNull()
    expect(parseRelayServerEvent({ ...event, session: 'x'.repeat(513) })).toBeNull()
    expect(parseRelayServerEvent({ ...event, v: 2 })).toBeNull()
    expect(parseRelayServerEvent({ ...event, type: 'unknown_node_event' })).toBeNull()
    expect(parseRelayServerEvent({ ...event, url: 'http://adapter.invalid/video' })).toBeNull()
  })

  test('accepts valid optional camera measurements and capture guidance', () => {
    expect(parseRelayServerEvent({ ...REJOIN_NODE_EVENTS[0], measured_hfov_deg: 65.5 })).not.toBeNull()
    expect(parseRelayServerEvent({
      ...REJOIN_NODE_EVENTS[2], room_id: 'room-1', capture_id: 'capture-1', guidance_mode: 'registered_metric',
      next_heading_deg: 90, coverage_missing: [0, 90, 180], suggested_delta: { kind: 'yaw', degrees: -15 },
    })).not.toBeNull()
  })

  test.each([undefined, 1, 2, 0, -1, 1.5, '2', Number.MAX_SAFE_INTEGER + 1])('validates state sequence %s', (sequence) => {
    const event = parseRelayServerEvent({
      v: 1, t: 100, type: 'state', event_id: 'sequence-test', session,
      roster_version: 1, state_sequence: sequence, armed: false, estop: false,
      selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null, accepted_plan: null, drones: [aircraft()],
    })
    if (sequence === undefined || sequence === 1 || sequence === 2) {
      expect(event).not.toBeNull()
    } else {
      expect(event).toBeNull()
    }
  })

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
        capability_profile: 'c1_basic_control',
        enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
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
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null,
      accepted_plan: null,
      drones: [aircraft()],
    })

    expect(event).not.toBeNull()
    expect(event?.type).toBe('state')
  })

  test.each([
    ['', ['hold']],
    ['custom', []],
    ['custom', ['hold', 'hold']],
    ['custom', ['unknown']],
    ['custom', ['disarm']],
    ['c1_basic_control', ['hold']],
  ])('rejects contradictory capability advertisement %s / %j', (profile, enabled) => {
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_000,
        type: 'state',
        event_id: 'bad-capability',
        session,
        roster_version: 4,
        armed: true,
        estop: false,
        selection: [1],
        formation: 'line',
        spacing: 0.8,
        mode: 'indoor',
        capability_profile: profile,
        enabled_intent_names: enabled,
        pending: null,
        accepted_plan: null,
        drones: [aircraft()],
      }),
    ).toBeNull()
  })

  test('accepts a bounded custom subset profile', () => {
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_000,
        type: 'state',
        event_id: 'land-only-capability',
        session,
        roster_version: 4,
        armed: true,
        estop: false,
        selection: [1],
        formation: 'line',
        spacing: 0.8,
        mode: 'indoor',
        capability_profile: 'land-only',
        enabled_intent_names: ['land'],
        pending: null,
        accepted_plan: null,
        drones: [aircraft()],
      }),
    ).toMatchObject({ capability_profile: 'land-only', enabled_intent_names: ['land'] })
  })

  test('accepts the deployment-derived C1 profile when altitude is ungrounded', () => {
    const enabled = C1_BASIC_CONTROL_INTENTS.filter((name) => name !== 'altitude')
    expect(
      parseRelayServerEvent({
        v: 1,
        t: 1_756_700_000_000,
        type: 'state',
        event_id: 'c1-no-altitude-capability',
        session,
        roster_version: 4,
        armed: true,
        estop: false,
        selection: [1],
        formation: 'line',
        spacing: 0.8,
        mode: 'indoor',
        capability_profile: 'c1_basic_control.no_altitude',
        enabled_intent_names: enabled,
        pending: null,
        accepted_plan: null,
        drones: [aircraft()],
      }),
    ).toMatchObject({
      capability_profile: 'c1_basic_control.no_altitude',
      enabled_intent_names: enabled,
    })
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
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
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

  test('accepts node-local safety actions as operator-visible evidence', () => {
    const event = parseRelayServerEvent({
      v: 1,
      t: 1_756_700_000_016,
      type: 'safety_action',
      event_id: 'safety-1',
      session,
      drone_id: 1,
      connection_epoch: 2,
      reason: 'link_loss',
      action: 'failsafe',
      loss_behavior: 'failsafe',
    })

    expect(event).toMatchObject({ type: 'safety_action', drone_id: 1, action: 'failsafe' })
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

describe('M1.4 production control intents', () => {
  test.each([
    ['arm', {}, [], false],
    ['select', { ids: [1, 2] }, [1, 2], false],
    ['takeoff', {}, [1, 2], true],
    ['translate', { dx: 1, dy: 0 }, [1, 2], false],
    ['hold', {}, [1, 2], false],
    ['come_home', {}, [1, 2], false],
    ['land_all', {}, [], true],
    ['estop', {}, [], false],
  ])('accepts the production %s envelope', (name, args, selection, confirm) => {
    expect(
      isConsoleIntentV1({
        v: 1,
        t: 1_756_700_000_000,
        type: 'intent',
        intent_id: `intent-${name}`,
        retry_of: null,
        source: 'console',
        session,
        name,
        args,
        selection,
        mode: 'indoor',
        confirm,
      }),
    ).toBe(true)
  })

  test('keeps takeoff and land-all behind confirmation', () => {
    for (const name of ['takeoff', 'land_all']) {
      expect(
        isConsoleIntentV1({
          v: 1,
          t: 1_756_700_000_000,
          type: 'intent',
          intent_id: `intent-${name}`,
          retry_of: null,
          source: 'console',
          session,
          name,
          args: {},
          selection: name === 'takeoff' ? [1, 2] : [],
          mode: 'indoor',
          confirm: false,
        }),
      ).toBe(false)
    }
  })
})

describe('console Intent v1 mirror', () => {
  test.each([
    ['console', 'hold', {}, [1], false],
    ['console', 'estop', {}, [], false],
    ['keyboard', 'estop', {}, [], false],
    ['webcam', 'hold', {}, [1], true],
    [
      'webcam',
      'capture_room',
      { room_id: 'room-1', capture_id: 'capture-1', pattern: 'pano_360' },
      [1],
      true,
    ],
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
    [
      'console',
      'sweep',
      { box: { min_x: -1, max_x: 1, min_y: -1, max_y: 1 } },
      [1],
      true,
    ],
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

  test('rejects ambiguous or unordered sweep boxes', () => {
    const base = {
      v: 1,
      t: 1_756_700_000_000,
      type: 'intent',
      intent_id: 'intent-sweep-box',
      retry_of: null,
      source: 'console',
      session,
      name: 'sweep',
      selection: [1],
      mode: 'indoor',
      confirm: true,
    }
    expect(isConsoleIntentV1({ ...base, args: { box: { x: 0, y: 0, w: 2, h: 2 } } })).toBe(false)
    expect(
      isConsoleIntentV1({
        ...base,
        args: { box: { min_x: 1, max_x: -1, min_y: -1, max_y: 1 } },
      }),
    ).toBe(false)
  })
})
