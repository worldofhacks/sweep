import { describe, expect, test } from 'vitest'
import type { IntentV1, RelayAircraftState, RelayServerEvent } from '../relay/contract'
import { retryIntent } from './intent'
import {
  controlReducer,
  createInitialControlState,
  createRequestRecord,
  type ControlState,
} from './state'

const session = 'session-state-test'
const t = 1_756_700_000_000

function drone(overrides: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return {
    drone_id: 1,
    connection_epoch: 1,
    membership: 'ready',
    readiness_reasons: [],
    flight_state: 'hovering',
    battery: 0.8,
    link: 0.9,
    pos_quality: 0.92,
    control_authority: true,
    last_seen_at: t,
    camera_patterns: ['pano_360', 'reconstruct_8'],
    selectable: true,
    adapter_id: 'adapter-1',
    adapter_capabilities: ['flight', 'camera:pano_360'],
    home_pose: { x: 0, y: 0, z: 0 },
    rc_safety_operator_present: true,
    telemetry: { state: 'hovering' },
    membership_history: [],
    ...overrides,
  }
}

function stateEvent(
  eventId: string,
  rosterVersion: number,
  drones: RelayAircraftState[],
  selection: number[],
): Extract<RelayServerEvent, { type: 'state' }> {
  return {
    v: 1,
    t: t + rosterVersion,
    type: 'state',
    event_id: eventId,
    session,
    roster_version: rosterVersion,
    armed: true,
    estop: false,
    selection,
    formation: 'none',
    spacing: 0.8,
    mode: 'indoor',
    pending: null,
    accepted_plan: null,
    drones,
  }
}

function captureIntent(id = 'intent-capture'): IntentV1 {
  return {
    v: 1,
    t,
    type: 'intent',
    intent_id: id,
    retry_of: null,
    source: 'console',
    session,
    name: 'capture_room',
    args: { room_id: 'room-1', capture_id: 'capture-1', pattern: 'pano_360' },
    selection: [1],
    mode: 'indoor',
    confirm: false,
  }
}

function withReadyState(): ControlState {
  return controlReducer(createInitialControlState(session, t), {
    type: 'relay_event',
    event: stateEvent('state-ready', 1, [drone()], [1]),
  })
}

function withPendingCapture(): ControlState {
  const intent = captureIntent()
  let state = withReadyState()
  state = controlReducer(state, {
    type: 'request_created',
    request: createRequestRecord(intent, t + 2),
  })
  return controlReducer(state, {
    type: 'request_pending_confirmation',
    intentId: intent.intent_id,
    t: t + 3,
    plan: { title: 'capture', steps: ['preview'], rosterVersion: 1 },
  })
}

describe('control reducer fleet lifecycle', () => {
  test('keeps the formation and spacing the relay reports, and nothing before the first frame', () => {
    const initial = createInitialControlState(session, t)
    expect(initial.formation).toBeNull()
    expect(initial.spacing).toBeNull()

    const event = stateEvent('state-formation', 1, [drone()], [1])
    event.formation = 'line'
    event.spacing = 1.5
    const state = controlReducer(initial, { type: 'relay_event', event })
    expect(state.formation).toBe('line')
    expect(state.spacing).toBe(1.5)
  })

  test('a roster-wide land_all preview survives selection changes but not roster changes', () => {
    const landAll: IntentV1 = {
      ...captureIntent('intent-land-all'),
      name: 'land_all',
      args: {},
      selection: [1, 2],
    }
    let state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: stateEvent('state-two', 1, [drone(), drone({ drone_id: 2 })], [1]),
    })
    state = controlReducer(state, {
      type: 'request_created',
      request: createRequestRecord(landAll, t + 2),
    })
    state = controlReducer(state, {
      type: 'request_pending_confirmation',
      intentId: landAll.intent_id,
      t: t + 3,
      plan: { title: 'Land all fleet', steps: ['land'], rosterVersion: 1 },
    })

    const selectionMoved = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent('state-selection-moved', 1, [drone(), drone({ drone_id: 2 })], [2]),
    })
    expect(selectionMoved.requests[0].status).toBe('pending_confirmation')

    const rosterMoved = controlReducer(selectionMoved, {
      type: 'relay_event',
      event: stateEvent('state-roster-moved', 2, [drone(), drone({ drone_id: 2 })], [2]),
    })
    expect(rosterMoved.requests[0]).toMatchObject({ status: 'invalidated', reasonCode: 'stale_roster' })
  })

  test('focuses a single authoritative selection and retains explicit focus through video loss', () => {
    let state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: stateEvent('state-single-selection', 1, [drone(), drone({ drone_id: 2 })], [1]),
    })
    expect(state.selectedFeedId).toBe(1)

    state = controlReducer(state, { type: 'feed_selected', droneId: 2 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent(
        'state-video-offline',
        1,
        [
          drone(),
          drone({
            drone_id: 2,
            video: { status: 'offline', last_frame_at: t - 2_000 },
          }),
        ],
        [1],
      ),
    })
    expect(state.selectedFeedId).toBe(2)

    state = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent('state-selection-changed', 1, [drone(), drone({ drone_id: 2 })], [2]),
    })
    expect(state.selectedFeedId).toBe(2)
  })

  test('reflects recovered telemetry while a local focus remains with no selection', () => {
    let state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: stateEvent(
        'state-telemetry-stale',
        1,
        [drone({ membership: 'degraded', selectable: false, readiness_reasons: ['telemetry_stale'] })],
        [],
      ),
    })
    expect(state.selectedFeedId).toBeNull()
    expect(state.aircraft[1].membership).toBe('degraded')

    state = controlReducer(state, { type: 'feed_selected', droneId: 1 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent('state-telemetry-recovered', 1, [drone()], []),
    })

    expect(state.selectedFeedId).toBe(1)
    expect(state.selection).toEqual([])
    expect(state.aircraft[1]).toMatchObject({ membership: 'ready', readiness_reasons: [] })
  })

  test('clears a stale selection and invalidates its pending preview on degraded state', () => {
    const pending = withPendingCapture()
    const next = controlReducer(pending, {
      type: 'relay_event',
      event: stateEvent(
        'state-degraded',
        1,
        [
          drone({
            membership: 'degraded',
            selectable: false,
            readiness_reasons: ['telemetry_stale'],
          }),
        ],
        [1],
      ),
    })

    expect(next.selection).toEqual([])
    expect(next.requests[0]).toMatchObject({
      status: 'invalidated',
      reasonCode: 'stale_selection',
    })
    expect(next.notices.some((notice) => notice.title === 'Stale selection cleared')).toBe(true)
  })

  test('invalidates a pending preview when a join changes roster version but not selection', () => {
    const pending = withPendingCapture()
    const next = controlReducer(pending, {
      type: 'relay_event',
      event: stateEvent('state-after-join', 2, [drone(), drone({ drone_id: 2 })], [1]),
    })

    expect(next.selection).toEqual([1])
    expect(next.requests[0]).toMatchObject({ status: 'invalidated', reasonCode: 'stale_roster' })
  })

  test('invalidates a preview as soon as a membership event advances the roster', () => {
    const pending = withPendingCapture()
    const next = controlReducer(pending, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 20,
        type: 'membership',
        event_id: 'join-before-state',
        session,
        action: 'join',
        drone_id: 2,
        connection_epoch: 1,
        membership: 'registered',
        roster_version: 2,
        reason: null,
        readiness_reasons: ['telemetry_missing'],
        adapter_id: 'adapter-2',
        capabilities: ['flight'],
        provenance: 'adapter_signature',
      },
    })

    expect(next.selection).toEqual([1])
    expect(next.aircraft[2].selectable).toBe(false)
    expect(next.requests[0]).toMatchObject({ status: 'invalidated', reasonCode: 'stale_roster' })
  })

  test('invalidates a preview when the authoritative selection changes', () => {
    const pending = withPendingCapture()
    const next = controlReducer(pending, {
      type: 'relay_event',
      event: stateEvent('state-selection-changed', 1, [drone(), drone({ drone_id: 2 })], [1, 2]),
    })

    expect(next.requests[0]).toMatchObject({
      status: 'invalidated',
      reasonCode: 'selection_changed',
    })
  })

  test('surfaces relay invalidation IDs and the graceful-leave reason', () => {
    const pending = withPendingCapture()
    const event = stateEvent('state-graceful-leave', 1, [drone()], [1])
    event.invalidated_intent_ids = ['intent-capture']
    event.invalidation_reason = 'graceful_leave_roster_change'
    event.prior_roster_version = 0
    event.cleared_control_fields = ['selection', 'pending', 'accepted_plan']
    const next = controlReducer(pending, { type: 'relay_event', event })

    expect(next.requests[0]).toMatchObject({
      status: 'invalidated',
      reasonCode: 'graceful_leave_roster_change',
      detail: 'Relay invalidated a plan after the fleet roster changed.',
    })
    expect(next.notices[0]).toMatchObject({ title: 'Plan invalidated' })
  })

  test('upgrades provisional roster invalidation from the paired graceful-leave state', () => {
    const pending = withPendingCapture()
    const afterMembership = controlReducer(pending, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 20,
        type: 'membership',
        event_id: 'graceful-leave-before-state',
        session,
        action: 'graceful_leave',
        drone_id: 1,
        connection_epoch: 1,
        membership: 'leaving',
        roster_version: 2,
        reason: 'graceful_leave_requested',
        readiness_reasons: ['leaving'],
        adapter_id: 'adapter-1',
        capabilities: ['flight', 'camera:pano_360'],
        provenance: 'adapter_signature',
      },
    })
    const provisionalTimestamp = afterMembership.requests[0].timestamps.invalidated
    expect(afterMembership.requests[0]).toMatchObject({
      status: 'invalidated',
      reasonCode: 'stale_roster',
    })

    const pairedState = stateEvent(
      'state-after-graceful-leave',
      2,
      [drone({ membership: 'leaving', selectable: false, readiness_reasons: ['leaving'] })],
      [],
    )
    pairedState.t = t + 20
    pairedState.invalidated_intent_ids = ['intent-capture']
    pairedState.invalidation_reason = 'graceful_leave_roster_change'
    pairedState.prior_roster_version = 1
    pairedState.cleared_control_fields = ['selection', 'pending', 'accepted_plan']
    const next = controlReducer(afterMembership, { type: 'relay_event', event: pairedState })

    expect(next.requests[0]).toMatchObject({
      status: 'invalidated',
      reasonCode: 'graceful_leave_roster_change',
      detail: 'Relay invalidated a plan after the fleet roster changed.',
    })
    expect(next.requests[0].timestamps.invalidated).toBe(provisionalTimestamp)
  })

  test('retains feed focus through video loss and aircraft departure until the ID disappears', () => {
    let state = controlReducer(withReadyState(), { type: 'feed_selected', droneId: 1 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent(
        'video-offline',
        1,
        [drone({ video: { status: 'offline', last_frame_at: t - 2_000 } })],
        [1],
      ),
    })
    expect(state.selectedFeedId).toBe(1)

    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 30,
        type: 'membership',
        event_id: 'feed-loss',
        session,
        action: 'unexpected_loss',
        drone_id: 1,
        connection_epoch: 1,
        membership: 'disconnected',
        roster_version: 2,
        reason: 'adapter_connection_lost',
        readiness_reasons: ['disconnected'],
        adapter_id: 'adapter-1',
        capabilities: ['flight'],
        provenance: 'relay_transport_attestation',
      },
    })
    expect(state.selectedFeedId).toBe(1)

    state = controlReducer(state, {
      type: 'relay_event',
      event: stateEvent('feed-removed', 2, [], []),
    })
    expect(state.selectedFeedId).toBeNull()
  })

  test('clears media state when an aircraft rejoins with a new membership epoch', () => {
    const current = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: stateEvent(
        'state-with-video',
        1,
        [drone({ video: { status: 'live', last_frame_at: t - 100 } })],
        [1],
      ),
    })

    const next = controlReducer(current, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 10,
        type: 'membership',
        event_id: 'rejoin-new-epoch',
        session,
        action: 'join',
        drone_id: 1,
        connection_epoch: 2,
        membership: 'registered',
        roster_version: 2,
        reason: 'adapter_connected',
        readiness_reasons: ['awaiting_readiness'],
        adapter_id: 'adapter-1',
        capabilities: ['flight', 'camera:pano_360'],
        provenance: 'relay_transport_attestation',
      },
    })

    expect(next.aircraft[1].connection_epoch).toBe(2)
    expect(next.aircraft[1].video).toBeUndefined()
  })

  test('preserves departed history and recognizes rejoin by a higher epoch', () => {
    const current = withReadyState()
    const lost = controlReducer(current, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 10,
        type: 'membership',
        event_id: 'loss-1',
        session,
        action: 'unexpected_loss',
        drone_id: 1,
        connection_epoch: 1,
        membership: 'disconnected',
        roster_version: 2,
        reason: 'adapter_connection_lost',
        readiness_reasons: ['disconnected'],
        adapter_id: 'adapter-1',
        capabilities: ['flight'],
        provenance: 'relay_transport_attestation',
      },
    })
    const rejoined = controlReducer(lost, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 11,
        type: 'membership',
        event_id: 'join-2',
        session,
        action: 'join',
        drone_id: 1,
        connection_epoch: 2,
        membership: 'registered',
        roster_version: 3,
        reason: null,
        readiness_reasons: ['telemetry_missing'],
        adapter_id: 'adapter-1',
        capabilities: ['flight'],
        provenance: 'adapter_signature',
      },
    })

    expect(lost.selection).toEqual([])
    expect(rejoined.departed).toHaveLength(1)
    expect(rejoined.aircraft[1]).toMatchObject({ connection_epoch: 2, selectable: false })
    expect(rejoined.notices.some((notice) => notice.title === 'D-01 rejoined')).toBe(true)
  })
})

describe('request lifecycle', () => {
  test('does not terminalize a request from one command in a multi-command plan', () => {
    const intent: IntentV1 = { ...captureIntent('intent-multi'), confirm: true }
    let state = withReadyState()
    state = controlReducer(state, {
      type: 'request_created',
      request: createRequestRecord(intent, t + 1),
    })
    state = controlReducer(state, { type: 'request_sent', intentId: intent.intent_id, t: t + 2 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 3,
        type: 'acknowledgement',
        event_id: 'command-ack-1',
        session,
        intent_id: intent.intent_id,
        command_id: 'command-1',
        status: 'completed',
        source: 'adapter',
        drone_id: 1,
        connection_epoch: 1,
        roster_version: 1,
        reason: null,
        detail: 'First camera step completed.',
      },
    })

    expect(state.requests[0].status).toBe('sent')
    expect(state.lastOutcome).toMatchObject({ commandId: 'command-1', status: 'completed' })

    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 4,
        type: 'acknowledgement',
        event_id: 'command-ack-2',
        session,
        intent_id: intent.intent_id,
        command_id: 'command-2',
        status: 'failed',
        source: 'adapter',
        drone_id: 1,
        connection_epoch: 1,
        roster_version: 1,
        reason: 'camera_timeout',
        detail: 'Second camera step failed.',
      },
    })

    expect(state.requests[0].status).toBe('sent')
    expect(state.lastOutcome).toMatchObject({ commandId: 'command-2', status: 'failed' })

    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 5,
        type: 'acknowledgement',
        event_id: 'intent-ack-1',
        session,
        intent_id: intent.intent_id,
        command_id: null,
        status: 'failed',
        source: 'relay',
        drone_id: null,
        connection_epoch: null,
        roster_version: 1,
        reason: 'command_failed',
        detail: 'Capture failed after an adapter command failure.',
      },
    })
    expect(state.requests[0].status).toBe('failed')
  })

  test('never terminalizes the request from an adapter fact with a null command ID', () => {
    const intent: IntentV1 = { ...captureIntent('intent-old-adapter'), confirm: true }
    let state = withReadyState()
    state = controlReducer(state, {
      type: 'request_created',
      request: createRequestRecord(intent, t + 1),
    })
    state = controlReducer(state, { type: 'request_sent', intentId: intent.intent_id, t: t + 2 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 3,
        type: 'acknowledgement',
        event_id: 'legacy-adapter-null-command',
        session,
        intent_id: intent.intent_id,
        command_id: null,
        status: 'completed',
        source: 'adapter',
        drone_id: 1,
        connection_epoch: 1,
        roster_version: 1,
        reason: null,
        detail: 'Malformed legacy adapter lifecycle fact.',
      },
    })

    expect(state.requests[0].status).toBe('sent')
    expect(state.lastOutcome).toMatchObject({ status: 'completed' })
  })

  test('never terminalizes the request from an adapter refusal with a null command ID', () => {
    const intent: IntentV1 = { ...captureIntent('intent-adapter-refusal'), confirm: true }
    let state = withReadyState()
    state = controlReducer(state, {
      type: 'request_created',
      request: createRequestRecord(intent, t + 1),
    })
    state = controlReducer(state, { type: 'request_sent', intentId: intent.intent_id, t: t + 2 })
    state = controlReducer(state, {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 3,
        type: 'refusal',
        event_id: 'legacy-adapter-null-command-refusal',
        session,
        intent_id: intent.intent_id,
        command_id: null,
        status: 'refused',
        source: 'adapter',
        drone_id: 1,
        connection_epoch: 1,
        roster_version: 1,
        reason: 'malformed_adapter_fact',
        detail: 'Malformed legacy adapter refusal.',
      },
    })

    expect(state.requests[0].status).toBe('sent')
    expect(state.lastOutcome).toMatchObject({
      kind: 'refusal',
      status: 'refused',
      reasonCode: 'malformed_adapter_fact',
    })
    expect(state.notices[0].title).toBe('Adapter command refused')
  })

  test('renders auth refusal as an unmatched visible failure', () => {
    const state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 1,
        type: 'auth.refused',
        event_id: 'auth-failed',
        session,
        status: 'refused',
        reason: 'invalid_token',
        detail: 'Authentication failed.',
      },
    })
    expect(state.lastOutcome).toMatchObject({ status: 'refused', reasonCode: 'invalid_token' })
    expect(state.notices[0].title).toBe('Relay authentication refused')
  })

  test('retains an uncorrelated protocol refusal in the visible last outcome', () => {
    const state = controlReducer(createInitialControlState(session, t), {
      type: 'relay_event',
      event: {
        v: 1,
        t: t + 1,
        type: 'refusal',
        event_id: 'protocol-refusal',
        session,
        intent_id: null,
        command_id: null,
        status: 'refused',
        source: 'relay',
        drone_id: null,
        connection_epoch: null,
        roster_version: 1,
        reason: 'invalid_payload',
        detail: 'Intent frame was malformed.',
      },
    })

    expect(state.lastOutcome).toMatchObject({
      status: 'refused',
      reasonCode: 'invalid_payload',
      detail: 'Intent frame was malformed.',
    })
    expect(state.notices[0].title).toBe('Unmatched relay result')
  })

  test('retries use a new ID, link the failed request, and require fresh confirmation', () => {
    const original: IntentV1 = { ...captureIntent('failed-intent'), confirm: true }
    const retry = retryIntent(original, { now: () => t + 50, nextId: () => 'retry-intent' })

    expect(retry).toMatchObject({
      intent_id: 'retry-intent',
      retry_of: 'failed-intent',
      confirm: false,
      t: t + 50,
    })
    expect(retry.args).toEqual(original.args)
  })
})
