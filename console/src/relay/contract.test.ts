import { describe, expect, test } from 'vitest'
import {
  C1_BASIC_CONTROL_INTENTS,
  MAX_INTENT_DRONE_ID,
  MAX_INTENT_DRONE_IDS,
  MAX_INTENT_IDENTIFIER_CODE_POINTS,
  MAX_INTENT_SESSION_CODE_POINTS,
  intentFromVoicePlanStep,
  isConsoleIntentV1,
  isVoicePlan,
  parseRelayServerEvent,
  type VoicePlan,
  type VoicePlanStep,
} from './contract'

const session = 'session-contract-test'
const t = 1_756_700_000_000

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
    membership_history_truncated: 0,
    ...overrides,
  }
}

describe('M1.1 wire compatibility', () => {
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

  test.each([
    [0, true],
    [12, true],
    [undefined, false],
    [-1, false],
    [1.5, false],
    ['1', false],
    [true, false],
  ])('validates the membership history truncation count %s', (truncated, accepted) => {
    const event = parseRelayServerEvent({
      v: 1, t: 100, type: 'state', event_id: 'history-truncation-test', session,
      roster_version: 1, state_sequence: 1, armed: false, estop: false,
      selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null, accepted_plan: null,
      drones: [aircraft({ membership_history_truncated: truncated })],
    })

    expect(event !== null).toBe(accepted)
  })

  test('normalizes the missing truncation count on persisted pre-change state', () => {
    const historicalAircraft = { ...aircraft() } as Record<string, unknown>
    delete historicalAircraft.membership_history_truncated

    const event = parseRelayServerEvent({
      v: 1, t: 100, type: 'state', event_id: 'historical-state', session,
      roster_version: 1, state_sequence: 1, armed: false, estop: false,
      selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor',
      capability_profile: 'c1_basic_control',
      enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
      pending: null, accepted_plan: null, drones: [historicalAircraft],
    })

    expect(event).not.toBeNull()
    expect(event?.type).toBe('state')
    if (event?.type !== 'state') throw new Error('expected a state event')
    expect(event.drones[0].membership_history_truncated).toBe(0)
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

  test('accepts only the exact operator-presence safety action variant', () => {
    const raw = {
      v: 1,
      t: 1_756_700_000_016,
      type: 'safety_action',
      event_id: 'presence-safety-1',
      session,
      reason: 'operator_presence_expired',
      action: 'hold',
      operator_last_seen_ms: 1_756_699_990_000,
      status: 'retrying',
      attempt: 2,
      intent_id: 'safety:operator-presence:1:2',
      targets: [{ drone_id: 1, connection_epoch: 2 }],
    }

    expect(parseRelayServerEvent(raw)).toEqual(raw)
    expect(parseRelayServerEvent({ ...raw, unexpected: true })).toBeNull()
    expect(parseRelayServerEvent({ ...raw, attempt: 0 })).toBeNull()
    expect(parseRelayServerEvent({ ...raw, targets: [] })).toBeNull()
    expect(
      parseRelayServerEvent({
        ...raw,
        status: 'not_required',
        attempt: 0,
        intent_id: null,
        targets: [],
      }),
    ).not.toBeNull()
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

  test('accepts exact Unicode, identifier, integer, and simulator-fleet ceilings', () => {
    expect(
      isConsoleIntentV1({
        v: 1,
        t: Number.MAX_SAFE_INTEGER,
        type: 'intent',
        intent_id: '🚁'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS),
        retry_of: 'r'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS),
        source: 'console',
        session: '🚁'.repeat(MAX_INTENT_SESSION_CODE_POINTS),
        name: 'select',
        args: { ids: Array.from({ length: MAX_INTENT_DRONE_IDS }, (_, index) => index + 1) },
        selection: Array.from({ length: MAX_INTENT_DRONE_IDS }, (_, index) => index + 1),
        mode: 'indoor',
        confirm: false,
      }),
    ).toBe(true)
  })

  test.each([
    { intent_id: 'i'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS + 1) },
    { retry_of: 'r'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS + 1) },
    { session: 's'.repeat(MAX_INTENT_SESSION_CODE_POINTS + 1) },
    { t: Number.MAX_SAFE_INTEGER + 1 },
    { selection: Array.from({ length: MAX_INTENT_DRONE_IDS + 1 }, (_, index) => index + 1) },
    { selection: [MAX_INTENT_DRONE_ID + 1] },
    { args: { ids: Array.from({ length: MAX_INTENT_DRONE_IDS + 1 }, (_, index) => index + 1) } },
    { intent_id: ' padded' },
    { intent_id: 'zero\u200bwidth' },
  ])('rejects Intent v1 producer values outside the relay boundary: %o', (override) => {
    expect(
      isConsoleIntentV1({
        v: 1,
        t,
        type: 'intent',
        intent_id: 'bounded-intent',
        retry_of: null,
        source: 'console',
        session,
        name: 'select',
        args: { ids: [1] },
        selection: [1],
        mode: 'indoor',
        confirm: false,
        ...override,
      }),
    ).toBe(false)
  })

  test('bounds capture identifiers at the shared relay ceiling', () => {
    const base = {
      v: 1,
      t,
      type: 'intent',
      intent_id: 'bounded-capture',
      retry_of: null,
      source: 'console',
      session,
      name: 'capture_room',
      selection: [1],
      mode: 'indoor',
      confirm: true,
    }
    expect(
      isConsoleIntentV1({
        ...base,
        args: {
          room_id: 'r'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS),
          capture_id: 'c'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS),
          pattern: 'reconstruct_8',
        },
      }),
    ).toBe(true)
    expect(
      isConsoleIntentV1({
        ...base,
        args: {
          room_id: 'r'.repeat(MAX_INTENT_IDENTIFIER_CODE_POINTS + 1),
          capture_id: 'capture',
          pattern: 'reconstruct_8',
        },
      }),
    ).toBe(false)
  })
})

function voiceStep(index: number, overrides: Partial<VoicePlanStep> = {}): VoicePlanStep {
  return {
    index,
    intent_id: `voice-step-${index}`,
    name: 'hold',
    args: {},
    selection: [1],
    mode: 'indoor',
    confirm_required: false,
    notes: ['Hold D-01.'],
    ...overrides,
  }
}

function voicePlan(steps: VoicePlanStep[]): VoicePlan {
  return {
    v: 1,
    kind: 'plan',
    transcript: 'Hold position.',
    reason: null,
    detail: null,
    options: [],
    steps,
    compiled_at_ms: t,
    expires_at_ms: t + 30_000,
    state_event_id: 'state-voice-plan',
    roster_version: 1,
    session,
    correlation_id: 'voice-request-1',
    plan_digest: 'a'.repeat(64),
    model: 'claude-sonnet-5',
    prompt_schema_version: 'intent-v1-compiler-8',
    response_source: 'anthropic',
    pending_intent_id: null,
  }
}

describe('bound voice-plan mirror', () => {
  test('accepts eight canonical steps and rejects nine at the console boundary', () => {
    const eight = Array.from({ length: 8 }, (_, index) => voiceStep(index))
    expect(isVoicePlan(voicePlan(eight))).toBe(true)
    expect(isVoicePlan(voicePlan([...eight, voiceStep(8)]))).toBe(false)
  })

  test('requires exact SELECT targets, confirmation policy, and lowercase digest', () => {
    const select = voiceStep(0, {
      name: 'select',
      args: { ids: [2] },
      selection: [2],
    })
    expect(isVoicePlan(voicePlan([select]))).toBe(true)
    expect(isVoicePlan(voicePlan([{ ...select, selection: [1] }]))).toBe(false)
    expect(isVoicePlan(voicePlan([{ ...select, confirm_required: true }]))).toBe(false)
    expect(isVoicePlan({ ...voicePlan([select]), plan_digest: 'A'.repeat(64) })).toBe(false)
  })

  test('mints a language draft only from the exact step object in the parsed plan', () => {
    const step = voiceStep(0, { intent_id: 'voice-bound-step' })
    const plan = voicePlan([step])
    const draft = intentFromVoicePlanStep(plan, step, t + 1)

    expect(draft).toMatchObject({
      intent_id: 'voice-bound-step',
      source: 'language',
      session,
      name: 'hold',
      selection: [1],
      confirm: false,
    })
    expect(intentFromVoicePlanStep(plan, { ...step }, t + 1)).toBeNull()
  })
})
