import type {
  BackendIntentStatus,
  CapturePattern,
  DroneId,
  IntentV1,
  IntentSource,
  MembershipAction,
  RelayAircraftState,
  RelayServerEvent,
} from '../relay/contract'
import { followsSelection } from '../relay/contract'

export type ConnectionStatus = 'connecting' | 'connected' | 'degraded' | 'disconnected'
export type RelayTransport = 'websocket' | 'fixture' | 'unavailable'
export type ClientIntentStatus = 'draft' | 'pending_confirmation' | 'sent'
export type RequestStatus =
  | ClientIntentStatus
  | BackendIntentStatus
  | 'cancelled'

export interface RelayConnection {
  status: ConnectionStatus
  transport: RelayTransport
  changedAt: number
  reason?: string
}

export interface PlanPreview {
  title: string
  steps: string[]
  rosterVersion: number
  /**
   * Wall-clock deadline for confirming this preview. Set only when the relay
   * reports a confirmation window; nothing sets it today, so the dock shows
   * no countdown.
   */
  expiresAt?: number
}

export interface RequestRecord {
  intent: IntentV1
  status: RequestStatus
  timestamps: Partial<Record<RequestStatus, number>>
  reasonCode?: string
  detail?: string
  plan?: PlanPreview
  responseRosterVersion?: number
}

export interface DepartureRecord {
  drone: RelayAircraftState
  action: Extract<MembershipAction, 'graceful_leave_completed' | 'unexpected_loss'>
  t: number
  reasonCode: string
  detail: string
}

export interface OutcomeSummary {
  kind: 'acknowledgement' | 'refusal' | 'failure' | 'invalidation'
  intentId: string
  status: RequestStatus
  reasonCode?: string
  detail: string
  t: number
  droneId?: DroneId
  connectionEpoch?: number
  commandId?: string
}

export interface OperatorNotice {
  id: string
  level: 'info' | 'warning' | 'danger'
  title: string
  detail: string
  t: number
}

export interface ControlState {
  sessionId: string
  connection: RelayConnection
  keyboardConnection: RelayConnection
  webcamConnection: RelayConnection
  rosterVersion: number
  aircraft: Record<DroneId, RelayAircraftState>
  selection: DroneId[]
  /** Formation and spacing the relay reports in its state frame; null until the first frame. */
  formation: string | null
  spacing: number | null
  departed: DepartureRecord[]
  requests: RequestRecord[]
  selectedFeedId: DroneId | null
  capturePattern: CapturePattern
  armed: boolean
  estop: boolean
  lastOutcome: OutcomeSummary | null
  notices: OperatorNotice[]
  seenEventIds: string[]
  lastStateEvent: { rosterVersion: number; t: number; source: IntentSource | null; sequence?: number } | null
}

export type ControlAction =
  | { type: 'connection_changed'; connection: RelayConnection }
  | { type: 'keyboard_connection_changed'; connection: RelayConnection }
  | { type: 'webcam_connection_changed'; connection: RelayConnection }
  | { type: 'relay_event'; event: RelayServerEvent; source?: IntentSource }
  | { type: 'request_created'; request: RequestRecord }
  | { type: 'request_pending_confirmation'; intentId: string; t: number; plan: PlanPreview }
  | { type: 'request_confirmed'; intent: IntentV1; t: number }
  | { type: 'request_sent'; intentId: string; t: number }
  | { type: 'request_send_failed'; intentId: string; t: number; detail: string }
  | { type: 'request_cancelled'; intentId: string; t: number }
  | { type: 'request_invalidated'; intentId: string; t: number; reasonCode: string; detail: string }
  | { type: 'capture_pattern_changed'; pattern: CapturePattern }
  | { type: 'feed_selected'; droneId: DroneId }

export function createInitialControlState(sessionId: string, now = Date.now()): ControlState {
  return {
    sessionId,
    connection: {
      status: 'disconnected',
      transport: 'unavailable',
      changedAt: now,
      reason: 'Relay runtime configuration is unavailable.',
    },
    keyboardConnection: {
      status: 'disconnected',
      transport: 'unavailable',
      changedAt: now,
      reason: 'Keyboard relay source is unavailable.',
    },
    webcamConnection: {
      status: 'disconnected',
      transport: 'unavailable',
      changedAt: now,
      reason: 'Webcam relay source is unavailable.',
    },
    rosterVersion: 0,
    aircraft: {},
    selection: [],
    formation: null,
    spacing: null,
    departed: [],
    requests: [],
    selectedFeedId: null,
    capturePattern: 'pano_360',
    armed: false,
    estop: false,
    lastOutcome: null,
    notices: [],
    seenEventIds: [],
    lastStateEvent: null,
  }
}

export function createRequestRecord(intent: IntentV1, now: number): RequestRecord {
  return {
    intent,
    status: 'draft',
    timestamps: { draft: now },
  }
}

export function controlReducer(state: ControlState, action: ControlAction): ControlState {
  switch (action.type) {
    case 'connection_changed':
      return reduceConnection(state, action.connection)
    case 'keyboard_connection_changed':
      return reduceKeyboardConnection(state, action.connection)
    case 'webcam_connection_changed':
      return reduceWebcamConnection(state, action.connection)
    case 'relay_event':
      return reduceRelayEvent(state, action.event, action.source ?? 'console')
    case 'request_created':
      return { ...state, requests: [action.request, ...state.requests] }
    case 'request_pending_confirmation':
      return updateRequest(state, action.intentId, (request) => ({
        ...request,
        status: 'pending_confirmation',
        timestamps: { ...request.timestamps, pending_confirmation: action.t },
        plan: action.plan,
      }))
    case 'request_confirmed':
      return updateRequest(state, action.intent.intent_id, (request) => ({
        ...request,
        intent: action.intent,
      }))
    case 'request_sent':
      return updateRequest(state, action.intentId, (request) => ({
        ...request,
        status: 'sent',
        timestamps: { ...request.timestamps, sent: action.t },
      }))
    case 'request_send_failed':
      return reduceSendFailure(state, action.intentId, action.t, action.detail)
    case 'request_cancelled':
      return updateRequest(state, action.intentId, (request) => ({
        ...request,
        status: 'cancelled',
        timestamps: { ...request.timestamps, cancelled: action.t },
        detail: 'Cancelled by the operator before dispatch.',
      }))
    case 'request_invalidated':
      return invalidateRequests(
        state,
        [action.intentId],
        action.t,
        action.reasonCode,
        action.detail,
      )
    case 'capture_pattern_changed':
      return { ...state, capturePattern: action.pattern }
    case 'feed_selected':
      return { ...state, selectedFeedId: action.droneId }
  }
}

function reduceConnection(state: ControlState, connection: RelayConnection): ControlState {
  if (connection.status === 'connected') return { ...state, connection }
  if (connection.status === 'connecting') return { ...state, connection }

  const level = connection.status === 'degraded' ? 'warning' : 'danger'
  const notice = makeNotice(
    `connection-${connection.changedAt}`,
    level,
    connection.status === 'degraded' ? 'Relay degraded' : 'Relay disconnected',
    connection.reason ?? 'No reason was provided.',
    connection.changedAt,
  )
  return { ...state, connection, notices: prependNotice(state.notices, notice) }
}

function reduceKeyboardConnection(state: ControlState, connection: RelayConnection): ControlState {
  if (connection.status === 'connected' || connection.status === 'connecting') {
    return { ...state, keyboardConnection: connection }
  }
  const notice = makeNotice(
    `keyboard-connection-${connection.changedAt}`,
    connection.status === 'degraded' ? 'warning' : 'danger',
    connection.status === 'degraded' ? 'Keyboard source degraded' : 'Keyboard stop unavailable',
    connection.reason ?? 'No reason was provided.',
    connection.changedAt,
  )
  return {
    ...state,
    keyboardConnection: connection,
    notices: prependNotice(state.notices, notice),
  }
}

function reduceWebcamConnection(state: ControlState, connection: RelayConnection): ControlState {
  if (connection.status === 'connected' || connection.status === 'connecting') {
    return { ...state, webcamConnection: connection }
  }
  const notice = makeNotice(
    `webcam-connection-${connection.changedAt}`,
    connection.status === 'degraded' ? 'warning' : 'danger',
    connection.status === 'degraded' ? 'Webcam source degraded' : 'Webcam source unavailable',
    connection.reason ?? 'No reason was provided.',
    connection.changedAt,
  )
  return {
    ...state,
    webcamConnection: connection,
    notices: prependNotice(state.notices, notice),
  }
}

function reduceRelayEvent(
  state: ControlState,
  event: RelayServerEvent,
  source: IntentSource,
): ControlState {
  if (state.seenEventIds.includes(event.event_id)) return state
  const stateWithEvent = {
    ...state,
    seenEventIds: [event.event_id, ...state.seenEventIds].slice(0, 256),
  }
  if (event.session !== state.sessionId) {
    return {
      ...stateWithEvent,
      notices: prependNotice(
        state.notices,
        makeNotice(
          `wrong-session-${event.t}`,
          'warning',
          'Ignored relay event',
          `Event belongs to session ${event.session}; this console is ${state.sessionId}.`,
          event.t,
        ),
      ),
    }
  }

  switch (event.type) {
    case 'auth.accepted':
      return stateWithEvent
    case 'auth.refused':
      return reduceAuthRefusal(stateWithEvent, event)
    case 'state':
      return reduceStateEvent(stateWithEvent, event, source)
    case 'membership':
      return reduceMembershipEvent(stateWithEvent, event)
    case 'telemetry':
      // The relay atomically follows telemetry with its authoritative state
      // projection. Retain the event ID for dedupe, but do not build a second
      // client-side source of aircraft truth here.
      return stateWithEvent
    case 'safety_action':
      return {
        ...stateWithEvent,
        notices: prependNotice(
          stateWithEvent.notices,
          makeNotice(
            `safety-action-${event.event_id}`,
            'danger',
            event.action === 'failsafe' ? 'Aircraft failsafe' : 'Aircraft hold',
            `D-${String(event.drone_id).padStart(2, '0')} applied ${event.action} after ${event.reason}.`,
            event.t,
          ),
        ),
      }
    case 'acknowledgement':
      if (event.command_id !== null || event.source === 'adapter') {
        return reduceCommandAcknowledgement(stateWithEvent, event)
      }
      return reduceBackendUpdate(stateWithEvent, {
        intentId: event.intent_id,
        status: event.status,
        t: event.t,
        reasonCode: event.reason ?? undefined,
        detail: event.detail ?? undefined,
        rosterVersion: event.roster_version,
        droneId: event.drone_id ?? undefined,
        connectionEpoch: event.connection_epoch ?? undefined,
      })
    case 'refusal':
      if (event.source === 'adapter') {
        return reduceAdapterRefusal(stateWithEvent, event)
      }
      return reduceBackendUpdate(stateWithEvent, {
        intentId: event.intent_id ?? `unmatched-${event.event_id}`,
        status: 'refused',
        t: event.t,
        reasonCode: event.reason ?? undefined,
        detail: event.detail,
        rosterVersion: event.roster_version,
        droneId: event.drone_id ?? undefined,
        connectionEpoch: event.connection_epoch ?? undefined,
      })
  }
}

function reduceAdapterRefusal(
  state: ControlState,
  event: Extract<RelayServerEvent, { type: 'refusal' }>,
): ControlState {
  const detail = event.detail || defaultStatusDetail('refused')
  return {
    ...state,
    lastOutcome: {
      kind: 'refusal',
      intentId: event.intent_id ?? `unmatched-${event.event_id}`,
      commandId: event.command_id ?? undefined,
      status: 'refused',
      reasonCode: event.reason,
      detail,
      t: event.t,
      droneId: event.drone_id ?? undefined,
      connectionEpoch: event.connection_epoch ?? undefined,
    },
    notices: prependNotice(
      state.notices,
      makeNotice(
        `adapter-refusal-${event.event_id}`,
        'danger',
        'Adapter command refused',
        `${event.reason}: ${detail}`,
        event.t,
      ),
    ),
  }
}

function reduceCommandAcknowledgement(
  state: ControlState,
  event: Extract<RelayServerEvent, { type: 'acknowledgement' }>,
): ControlState {
  const detail = event.detail ?? defaultStatusDetail(event.status)
  const isFailure = event.status === 'failed' || event.status === 'invalidated'
  const next: ControlState = {
    ...state,
    lastOutcome: {
      kind: isFailure
        ? event.status === 'failed'
          ? 'failure'
          : 'invalidation'
        : 'acknowledgement',
      intentId: event.intent_id,
      commandId: event.command_id ?? undefined,
      status: event.status,
      reasonCode: event.reason ?? undefined,
      detail,
      t: event.t,
      droneId: event.drone_id ?? undefined,
      connectionEpoch: event.connection_epoch ?? undefined,
    },
  }
  if (!isFailure) return next
  return {
    ...next,
    notices: prependNotice(
      next.notices,
      makeNotice(
        `command-${event.status}-${event.event_id}`,
        event.status === 'failed' ? 'danger' : 'warning',
        event.status === 'failed' ? 'Adapter command failed' : 'Adapter command invalidated',
        `${event.reason ? `${event.reason}: ` : ''}${detail}`,
        event.t,
      ),
    ),
  }
}

function reduceStateEvent(
  state: ControlState,
  event: Extract<RelayServerEvent, { type: 'state' }>,
  source: IntentSource,
): ControlState {
  const lastStateEvent = state.lastStateEvent
  const sequenced = event.state_sequence !== undefined
  if (lastStateEvent?.sequence !== undefined &&
    (event.state_sequence === undefined || event.state_sequence <= lastStateEvent.sequence)) return state
  if (
    event.roster_version < state.rosterVersion ||
    (!sequenced && lastStateEvent !== null &&
      event.roster_version === lastStateEvent.rosterVersion &&
      event.t < lastStateEvent.t)
  ) {
    return state
  }
  // Equal timestamps order one socket, but cannot order competing socket snapshots.
  const ambiguousOrder = !sequenced && lastStateEvent !== null &&
    event.roster_version === lastStateEvent.rosterVersion &&
    event.t === lastStateEvent.t && lastStateEvent.source !== source
  const aircraft = Object.fromEntries(event.drones.map((drone) => [drone.drone_id, drone]))
  const staleSelection = event.selection.filter(
    (id) => aircraft[id]?.membership !== 'ready' || !aircraft[id]?.selectable,
  )
  const selection = event.selection.filter(
    (id) => aircraft[id]?.membership === 'ready' && aircraft[id]?.selectable,
  )
  let next: ControlState = {
    ...state,
    rosterVersion: event.roster_version,
    aircraft,
    selection,
    formation: event.formation,
    spacing: event.spacing,
    armed: event.armed,
    estop: event.estop || (ambiguousOrder && state.estop),
    lastStateEvent: {
      rosterVersion: event.roster_version,
      t: event.t,
      source: ambiguousOrder ? null : source,
      sequence: event.state_sequence,
    },
  }

  if (
    selection.length === 1 &&
    (state.selectedFeedId === null || !sameDroneSet(state.selection, selection))
  ) {
    next = { ...next, selectedFeedId: selection[0] }
  }

  if (
    state.selectedFeedId !== null &&
    !aircraft[state.selectedFeedId]
  ) {
    next = { ...next, selectedFeedId: null }
  }
  if (event.invalidated_intent_ids?.length) {
    const invalidationDetail = 'Relay invalidated a plan after the fleet roster changed.'
    next = invalidateRequests(
      next,
      event.invalidated_intent_ids,
      event.t,
      event.invalidation_reason ?? 'stale_roster',
      invalidationDetail,
    )
    if (event.invalidation_reason === 'graceful_leave_roster_change') {
      next = upgradeProvisionalInvalidationReason(
        next,
        event.invalidated_intent_ids,
        event.invalidation_reason,
        invalidationDetail,
      )
    }
  }
  if (staleSelection.length > 0) {
    next = addStaleSelectionNotice(next, staleSelection, event.t)
    const invalidatedIds = next.requests
      .filter(
        (request) =>
          request.status === 'pending_confirmation' &&
          followsSelection(request.intent.name) &&
          request.intent.selection.some((id) => staleSelection.includes(id)),
      )
      .map((request) => request.intent.intent_id)
    next = invalidateRequests(
      next,
      invalidatedIds,
      event.t,
      'stale_selection',
      'An aircraft in the pending preview is no longer ready or selectable.',
    )
  }
  const staleRosterRequests = next.requests
    .filter(
      (request) =>
        request.status === 'pending_confirmation' &&
        request.plan?.rosterVersion !== event.roster_version,
    )
    .map((request) => request.intent.intent_id)
  next = invalidateRequests(
    next,
    staleRosterRequests,
    event.t,
    'stale_roster',
    `Fleet roster changed to version ${event.roster_version}. Build and confirm a new preview.`,
  )
  // Intents that address the whole roster (land_all) keep their preview while
  // the operator's selection moves; only a roster change invalidates them.
  const changedSelectionRequests = next.requests
    .filter(
      (request) =>
        request.status === 'pending_confirmation' &&
        followsSelection(request.intent.name) &&
        !sameDroneSet(request.intent.selection, selection),
    )
    .map((request) => request.intent.intent_id)
  next = invalidateRequests(
    next,
    changedSelectionRequests,
    event.t,
    'selection_changed',
    'The authoritative aircraft selection changed. Build and confirm a new preview.',
  )
  return next
}

function reduceMembershipEvent(
  state: ControlState,
  event: Extract<RelayServerEvent, { type: 'membership' }>,
): ControlState {
  if (
    event.roster_version < state.rosterVersion ||
    (state.lastStateEvent !== null && event.roster_version <= state.lastStateEvent.rosterVersion)
  ) return state
  const previous = state.aircraft[event.drone_id]
  const drone = projectMembershipEvent(event, previous)
  const aircraft = { ...state.aircraft, [event.drone_id]: drone }
  const isDeparture =
    event.action === 'graceful_leave_completed' || event.action === 'unexpected_loss'
  const wasSelected = state.selection.includes(event.drone_id)
  const selection =
    isDeparture || event.membership !== 'ready'
      ? state.selection.filter((id) => id !== event.drone_id)
      : state.selection
  let next: ControlState = {
    ...state,
    rosterVersion: event.roster_version,
    aircraft,
    selection,
    selectedFeedId: state.selectedFeedId,
  }

  const staleRosterRequests = next.requests
    .filter(
      (request) =>
        request.status === 'pending_confirmation' &&
        request.plan?.rosterVersion !== event.roster_version,
    )
    .map((request) => request.intent.intent_id)
  next = invalidateRequests(
    next,
    staleRosterRequests,
    event.t,
    'stale_roster',
    `Fleet roster changed to version ${event.roster_version}. Build and confirm a new preview.`,
  )

  if (isDeparture) {
    const departure: DepartureRecord = {
      drone,
      action:
        event.action === 'graceful_leave_completed'
          ? 'graceful_leave_completed'
          : 'unexpected_loss',
      t: event.t,
      reasonCode: event.reason ?? event.action,
      detail:
        event.action === 'graceful_leave_completed'
          ? 'Aircraft completed a graceful leave.'
          : 'Aircraft connection was lost unexpectedly.',
    }
    next = { ...next, departed: [departure, ...next.departed] }
    next = invalidateRequestsForDrone(next, event.drone_id, event.t, departure.reasonCode, departure.detail)
  }

  if (wasSelected && !selection.includes(event.drone_id)) {
    next = addStaleSelectionNotice(next, [event.drone_id], event.t)
  }
  if (
    event.action === 'join' &&
    previous &&
    event.connection_epoch > previous.connection_epoch
  ) {
    next = {
      ...next,
      notices: prependNotice(
        next.notices,
        makeNotice(
          `rejoin-${event.event_id}`,
          'info',
          `${formatDroneId(event.drone_id)} rejoined`,
          `Connection epoch is now ${event.connection_epoch}. Selection was not changed.`,
          event.t,
        ),
      ),
    }
  }
  return next
}

function projectMembershipEvent(
  event: Extract<RelayServerEvent, { type: 'membership' }>,
  previous?: RelayAircraftState,
): RelayAircraftState {
  return {
    drone_id: event.drone_id,
    connection_epoch: event.connection_epoch,
    membership: event.membership,
    readiness_reasons: [...event.readiness_reasons],
    flight_state: previous?.flight_state ?? null,
    battery: previous?.battery ?? null,
    link: previous?.link ?? null,
    pos_quality: previous?.pos_quality ?? null,
    control_authority: previous?.control_authority ?? false,
    last_seen_at: previous?.last_seen_at ?? null,
    camera_patterns: previous?.camera_patterns ?? [],
    selectable: false,
    adapter_id: event.adapter_id ?? previous?.adapter_id ?? 'adapter-unreported',
    adapter_capabilities: [...event.capabilities],
    home_pose: previous?.home_pose ?? null,
    rc_safety_operator_present: previous?.rc_safety_operator_present ?? false,
    telemetry: previous?.telemetry ?? null,
    membership_history: previous?.membership_history ?? [],
    video: previous?.connection_epoch === event.connection_epoch ? previous.video : undefined,
  }
}

function reduceAuthRefusal(
  state: ControlState,
  event: Extract<RelayServerEvent, { type: 'auth.refused' }>,
): ControlState {
  const detail = `${event.reason}: ${event.detail}`
  return {
    ...state,
    lastOutcome: {
      kind: 'refusal',
      intentId: `auth-${event.event_id}`,
      status: 'refused',
      reasonCode: event.reason,
      detail: event.detail,
      t: event.t,
    },
    notices: prependNotice(
      state.notices,
      makeNotice(`auth-refused-${event.event_id}`, 'danger', 'Relay authentication refused', detail, event.t),
    ),
  }
}

interface BackendUpdate {
  intentId: string
  status: BackendIntentStatus
  t: number
  reasonCode?: string
  detail?: string
  rosterVersion?: number
  droneId?: DroneId
  connectionEpoch?: number
}

function reduceBackendUpdate(state: ControlState, update: BackendUpdate): ControlState {
  const request = state.requests.find((item) => item.intent.intent_id === update.intentId)
  if (!request) {
    const detail = update.detail ?? defaultStatusDetail(update.status)
    const outcomeKind = outcomeKindForStatus(update.status)
    return {
      ...state,
      lastOutcome: {
        kind: outcomeKind,
        intentId: update.intentId,
        status: update.status,
        reasonCode: update.reasonCode,
        detail,
        t: update.t,
        droneId: update.droneId,
        connectionEpoch: update.connectionEpoch,
      },
      notices: prependNotice(
        state.notices,
        makeNotice(
          `unknown-intent-${update.intentId}-${update.t}`,
          outcomeKind === 'failure' || outcomeKind === 'refusal' ? 'danger' : 'warning',
          'Unmatched relay result',
          `No local request matches intent ${update.intentId}. ${update.reasonCode ? `${update.reasonCode}: ` : ''}${detail}`,
          update.t,
        ),
      ),
    }
  }

  const detail = update.detail ?? defaultStatusDetail(update.status)
  const outcomeKind = outcomeKindForStatus(update.status)
  const requests = state.requests.map((item) =>
    item.intent.intent_id === update.intentId
      ? {
          ...item,
          status: update.status,
          timestamps: { ...item.timestamps, [update.status]: update.t },
          reasonCode: update.reasonCode,
          detail,
          responseRosterVersion: update.rosterVersion ?? item.responseRosterVersion,
        }
      : item,
  )
  const lastOutcome: OutcomeSummary = {
    kind: outcomeKind,
    intentId: update.intentId,
    status: update.status,
    reasonCode: update.reasonCode,
    detail,
    t: update.t,
    droneId: update.droneId,
    connectionEpoch: update.connectionEpoch,
  }
  const next = { ...state, requests, lastOutcome }

  if (outcomeKind === 'acknowledgement') return next
  return {
    ...next,
    notices: prependNotice(
      next.notices,
      makeNotice(
        `${update.status}-${update.intentId}-${update.t}`,
        update.status === 'invalidated' ? 'warning' : 'danger',
        update.status === 'refused'
          ? 'Intent refused'
          : update.status === 'invalidated'
            ? 'Plan invalidated'
            : 'Request failed',
        `${update.reasonCode ? `${update.reasonCode}: ` : ''}${detail}`,
        update.t,
      ),
    ),
  }
}

function reduceSendFailure(
  state: ControlState,
  intentId: string,
  t: number,
  detail: string,
): ControlState {
  const updated = updateRequest(state, intentId, (request) => ({
    ...request,
    status: 'failed',
    timestamps: { ...request.timestamps, failed: t },
    reasonCode: 'send_failed',
    detail,
  }))
  return {
    ...updated,
    lastOutcome: {
      kind: 'failure',
      intentId,
      status: 'failed',
      reasonCode: 'send_failed',
      detail,
      t,
    },
    notices: prependNotice(
      updated.notices,
      makeNotice(`send-failed-${intentId}-${t}`, 'danger', 'Intent was not sent', detail, t),
    ),
  }
}

function invalidateRequestsForDrone(
  state: ControlState,
  droneId: DroneId,
  t: number,
  reasonCode: string,
  detail: string,
): ControlState {
  const ids = state.requests
    .filter(
      (request) =>
        request.status === 'pending_confirmation' && request.intent.selection.includes(droneId),
    )
    .map((request) => request.intent.intent_id)
  return invalidateRequests(state, ids, t, reasonCode, detail)
}

function invalidateRequests(
  state: ControlState,
  intentIds: string[],
  t: number,
  reasonCode: string,
  detail: string,
): ControlState {
  if (intentIds.length === 0) return state
  const idSet = new Set(intentIds)
  const requests = state.requests.map((request) =>
    idSet.has(request.intent.intent_id) && !isTerminalRequest(request.status)
      ? {
          ...request,
          status: 'invalidated' as const,
          timestamps: { ...request.timestamps, invalidated: t },
          reasonCode,
          detail,
        }
      : request,
  )
  const intentId = intentIds[0]
  return {
    ...state,
    requests,
    lastOutcome: {
      kind: 'invalidation',
      intentId,
      status: 'invalidated',
      reasonCode,
      detail,
      t,
    },
    notices: prependNotice(
      state.notices,
      makeNotice(`invalidated-${intentId}-${t}`, 'warning', 'Plan invalidated', `${reasonCode}: ${detail}`, t),
    ),
  }
}

function upgradeProvisionalInvalidationReason(
  state: ControlState,
  intentIds: string[],
  reasonCode: string,
  detail: string,
): ControlState {
  const idSet = new Set(intentIds)
  return {
    ...state,
    requests: state.requests.map((request) =>
      idSet.has(request.intent.intent_id) &&
      request.status === 'invalidated' &&
      request.reasonCode === 'stale_roster'
        ? { ...request, reasonCode, detail }
        : request,
    ),
  }
}

function addStaleSelectionNotice(state: ControlState, ids: DroneId[], t: number): ControlState {
  const labels = ids.map(formatDroneId).join(', ')
  return {
    ...state,
    notices: prependNotice(
      state.notices,
      makeNotice(
        `stale-selection-${ids.join('-')}-${t}`,
        'warning',
        'Stale selection cleared',
        `${labels} is no longer ready. No substitute aircraft was selected.`,
        t,
      ),
    ),
  }
}

function updateRequest(
  state: ControlState,
  intentId: string,
  update: (request: RequestRecord) => RequestRecord,
): ControlState {
  return {
    ...state,
    requests: state.requests.map((request) =>
      request.intent.intent_id === intentId ? update(request) : request,
    ),
  }
}

function prependNotice(notices: OperatorNotice[], notice: OperatorNotice): OperatorNotice[] {
  return [notice, ...notices.filter((item) => item.id !== notice.id)].slice(0, 8)
}

function makeNotice(
  id: string,
  level: OperatorNotice['level'],
  title: string,
  detail: string,
  t: number,
): OperatorNotice {
  return { id, level, title, detail, t }
}

function outcomeKindForStatus(status: BackendIntentStatus): OutcomeSummary['kind'] {
  if (status === 'refused') return 'refusal'
  if (status === 'failed') return 'failure'
  if (status === 'invalidated') return 'invalidation'
  return 'acknowledgement'
}

function defaultStatusDetail(status: BackendIntentStatus): string {
  switch (status) {
    case 'accepted':
      return 'Relay accepted the intent.'
    case 'refused':
      return 'Relay refused the intent.'
    case 'executing':
      return 'The accepted plan is executing.'
    case 'completed':
      return 'The request completed.'
    case 'failed':
      return 'The request failed.'
    case 'invalidated':
      return 'The accepted plan was invalidated.'
  }
}

function sameDroneSet(left: DroneId[], right: DroneId[]): boolean {
  return left.length === right.length && left.every((id) => right.includes(id))
}

export function isTerminalRequest(status: RequestStatus): boolean {
  return ['cancelled', 'completed', 'failed', 'invalidated', 'refused'].includes(status)
}

export function formatDroneId(id: DroneId): string {
  return `D-${String(id).padStart(2, '0')}`
}
