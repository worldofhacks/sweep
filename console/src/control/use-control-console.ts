import { useCallback, useEffect, useMemo, useReducer, useRef, type Dispatch } from 'react'
import type { RelayClient } from '../relay/client'
import type {
  CapturePattern,
  ConsoleIntentName,
  DroneId,
  IntentArgsByName,
  IntentSource,
  IntentV1,
  VoicePlan,
  VoicePlanStep,
} from '../relay/contract'
import {
  intentFromVoicePlanStep,
  followsSelection,
  isConsoleIntentV1,
  requiresConfirmation,
  selectionRule,
} from '../relay/contract'
import {
  browserIntentDependencies,
  confirmIntent,
  createCaptureArgs,
  createIntent,
  isValidRoomId,
  retryIntent,
  type IntentFactoryDependencies,
} from './intent'
import { buildPlanPreview } from './plan'
import {
  capabilityBlockedReason,
  controlReducer,
  createInitialControlState,
  createRequestRecord,
  isIntentEnabled,
  type ControlState,
  type RequestRecord,
} from './state'

export interface ControlClients {
  console: RelayClient
  keyboard: RelayClient
  /** Optional gesture producer source; drafts with source webcam are sent through it. */
  webcam?: RelayClient
  /** Optional relay-bound compiler source; raw language intents fail closed without it. */
  language?: RelayClient
}

/** Sources that draft previewed requests through the operator control flow. */
export type DraftSource = Extract<IntentSource, 'console' | 'webcam'>

export interface UseControlConsoleOptions {
  sessionId: string
  clients: ControlClients
  intentDependencies?: IntentFactoryDependencies
}

/** One control press: an intent name, its args, and the aircraft it addresses. */
export interface IntentRequest<N extends ConsoleIntentName = ConsoleIntentName> {
  name: N
  args: IntentArgsByName[N]
  /** Defaults to the authoritative selection. `land_all` passes the whole roster. */
  targets?: DroneId[]
}

export function useControlConsole({
  sessionId,
  clients,
  intentDependencies = browserIntentDependencies,
}: UseControlConsoleOptions) {
  const [state, dispatch] = useReducer(
    controlReducer,
    sessionId,
    (id) => createInitialControlState(id, intentDependencies.now()),
  )
  const confirmedIds = useRef(new Set<string>())

  // Only one pending preview can be confirmed. Retain its synchronous send
  // guard until React commits the lifecycle update, then release the entry.
  useEffect(() => {
    const pendingIds = new Set(state.requests.filter((request) => request.status === 'pending_confirmation').map((request) => request.intent.intent_id))
    for (const id of confirmedIds.current) if (!pendingIds.has(id)) confirmedIds.current.delete(id)
  }, [state.requests])
  useEffect(() => { confirmedIds.current.clear() }, [sessionId])

  useEffect(() => {
    if (state.sessionId !== sessionId) {
      dispatch({ type: 'session_changed', sessionId, t: intentDependencies.now() })
    }
  }, [intentDependencies, sessionId, state.sessionId])

  useEffect(() => {
    const unsubscribeConsole = clients.console.subscribe((event) => {
      if (event.kind === 'connection') {
        dispatch({ type: 'connection_changed', connection: event.connection })
      } else {
        dispatch({ type: 'relay_event', event: event.event, source: 'console' })
      }
    })
    const subscribeLifecycleOnly = (
      client: RelayClient,
      connectionType:
        | 'keyboard_connection_changed'
        | 'webcam_connection_changed'
        | 'language_connection_changed',
    ) =>
      client.subscribe((event) => {
        if (event.kind === 'connection') {
          dispatch({ type: connectionType, connection: event.connection })
          return
        }
        if (
          event.event.type === 'acknowledgement' ||
          event.event.type === 'refusal' ||
          event.event.type === 'auth.accepted' ||
          event.event.type === 'auth.refused' ||
          (connectionType === 'keyboard_connection_changed' &&
            (event.event.type === 'safety_action' || event.event.type === 'state'))
        ) {
          const source: IntentSource =
            connectionType === 'keyboard_connection_changed'
              ? 'keyboard'
              : connectionType === 'webcam_connection_changed'
                ? 'webcam'
                : 'language'
          dispatch({ type: 'relay_event', event: event.event, source })
        }
      })
    const unsubscribeKeyboard = subscribeLifecycleOnly(clients.keyboard, 'keyboard_connection_changed')
    const unsubscribeWebcam = clients.webcam
      ? subscribeLifecycleOnly(clients.webcam, 'webcam_connection_changed')
      : () => {}
    const unsubscribeLanguage = clients.language
      ? subscribeLifecycleOnly(clients.language, 'language_connection_changed')
      : () => {}

    clients.console.start()
    clients.keyboard.start()
    clients.webcam?.start()
    clients.language?.start()
    return () => {
      unsubscribeConsole()
      unsubscribeKeyboard()
      unsubscribeWebcam()
      unsubscribeLanguage()
      clients.console.stop()
      clients.keyboard.stop()
      clients.webcam?.stop()
      clients.language?.stop()
    }
  }, [clients])

  const clientFor = useCallback(
    (source: IntentSource): RelayClient | null => {
      if (source === 'keyboard') return clients.keyboard
      if (source === 'webcam') return clients.webcam ?? null
      if (source === 'language') return clients.language ?? null
      return clients.console
    },
    [clients],
  )

  /** Marks a recorded request sent and hands it to the client its source names. */
  const sendNow = useCallback(
    (intent: IntentV1, t: number) => {
      if (!isIntentEnabled(state, intent.name)) {
        dispatch({
          type: 'request_send_failed',
          intentId: intent.intent_id,
          t,
          detail: capabilityBlockedReason(state, intent.name) ?? 'The intent is disabled.',
        })
        return
      }
      dispatch({ type: 'request_sent', intentId: intent.intent_id, t })
      const client = clientFor(intent.source)
      if (!client) {
        dispatch({
          type: 'request_send_failed',
          intentId: intent.intent_id,
          t,
          detail: `No relay connection is bound to source ${intent.source}; the intent was not sent.`,
        })
        return
      }
      sendToRelay(intent, client, t, intentDependencies.now, dispatch)
    },
    [clientFor, intentDependencies, state],
  )

  /**
   * Records a freshly minted intent and parks it in the dock with its plan
   * preview. One preview at a time: a new draft cancels an earlier, unconfirmed
   * one. The intent id never changes. The speech compiler and the gesture
   * producer draft through this so nothing leaves on a compile or a pose. A
   * relay-compiled step passes its plan's deadline as `expiresAt`; the preview
   * carries it so the dock counts it down and confirmRequest honours it.
   */
  const stageForConfirmation = useCallback(
    (
      intent: IntentV1,
      expiresAt?: number,
      voiceBinding?: NonNullable<RequestRecord['plan']>['voiceBinding'],
    ): IntentV1 => {
      const t = intentDependencies.now()
      confirmedIds.current.delete(intent.intent_id)
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      state.requests
        .filter((request) => request.status === 'pending_confirmation')
        .forEach((request) => {
          dispatch({ type: 'request_cancelled', intentId: request.intent.intent_id, t })
        })
      dispatch({
        type: 'request_pending_confirmation',
        intentId: intent.intent_id,
        t,
        plan: buildPlanPreview(intent, state.rosterVersion, expiresAt, voiceBinding),
      })
      return intent
    },
    [intentDependencies, state.requests, state.rosterVersion],
  )

  /**
   * Records a freshly minted intent, then either parks it for confirmation
   * (with its plan preview) or sends it at once.
   */
  const stageIntent = useCallback(
    (intent: IntentV1, expiresAt?: number) => {
      if (intent.name === 'select') {
        state.requests.filter((request) => request.status === 'pending_confirmation').forEach((request) => {
          dispatch({ type: 'request_invalidated', intentId: request.intent.intent_id,
            t: intentDependencies.now(), reasonCode: 'selection_change_requested',
            detail: 'A new selection was requested. Preview the command again after relay state updates.' })
        })
      }
      if (requiresConfirmation(intent.name)) {
        stageForConfirmation(intent, expiresAt)
        return
      }
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      sendNow(intent, t)
    },
    [intentDependencies, sendNow, stageForConfirmation, state.requests],
  )

  const sendExistingIntent = useCallback(
    (intent: IntentV1, t: number) => {
      dispatch({ type: 'request_confirmed', intent, t })
      sendNow(intent, t)
    },
    [sendNow],
  )

  const issueIntent = useCallback(
    <N extends ConsoleIntentName>(request: IntentRequest<N>, expiresAt?: number): IntentV1 | null => {
      if (!isIntentEnabled(state, request.name)) return null
      const intent = createIntent(
        {
          name: request.name,
          args: request.args,
          selection: ['arm', 'land_all', 'estop'].includes(request.name) ? [] : request.targets ?? state.selection,
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      stageIntent(intent, expiresAt)
      return intent
    },
    [intentDependencies, stageIntent, state],
  )

  /**
   * A select intent addresses the aircraft it names: `selection` carries the
   * desired ids, the same as `args.ids`, so selecting from an empty selection
   * still satisfies the at-least-one rule.
   */
  const sendSelection = useCallback(
    (desired: DroneId[]) => {
      if (!isIntentEnabled(state, 'select') || desired.length === 0) return
      const intent = createIntent(
        {
          name: 'select',
          args: { ids: desired },
          selection: desired,
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      stageIntent(intent)
    },
    [intentDependencies, stageIntent, state],
  )

  /** Registry and mosaic toggles are additive and never empty the selection. */
  const toggleAircraft = useCallback(
    (droneId: DroneId) => {
      const aircraft = state.aircraft[droneId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return
      const isSelected = state.selection.includes(droneId)
      const desired = isSelected
        ? state.selection.filter((id) => id !== droneId)
        : [...state.selection, droneId].sort((a, b) => a - b)
      sendSelection(desired)
    },
    [sendSelection, state.aircraft, state.selection],
  )

  /**
   * Swarm chips are single-select: pressing an unselected chip replaces the
   * selection with that one aircraft; pressing a selected chip removes it
   * unless it is the last one.
   */
  const selectAircraft = useCallback(
    (droneId: DroneId) => {
      const aircraft = state.aircraft[droneId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return
      const desired = state.selection.includes(droneId)
        ? state.selection.filter((id) => id !== droneId)
        : [droneId]
      sendSelection(desired)
    },
    [sendSelection, state.aircraft, state.selection],
  )

  const selectAllReady = useCallback(() => {
    const ready = Object.values(state.aircraft)
      .filter((drone) => drone.membership === 'ready' && drone.selectable)
      .map((drone) => drone.drone_id)
      .sort((a, b) => a - b)
    sendSelection(ready)
  }, [sendSelection, state.aircraft])

  /**
   * Drafts a capture_room preview. The pattern defaults to the console's current
   * pattern; the speech compiler passes the pattern the utterance named, and the
   * gesture producer drafts with source webcam.
   */
  const prepareCapture = useCallback(
    (
      roomId: string,
      source: DraftSource = 'console',
      pattern: CapturePattern = state.capturePattern,
      expiresAt?: number,
    ): IntentV1 | null => {
      if (!state.enabledIntentNames.includes('capture_room')) return null
      const selectedId = state.selection[0]
      if (state.selection.length !== 1 || !selectedId) return null
      const aircraft = state.aircraft[selectedId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return null
      if (!aircraft.camera_patterns.includes(pattern)) return null
      const trimmedRoomId = roomId.trim()
      if (!isValidRoomId(trimmedRoomId)) return null

      const draft = createIntent(
        {
          name: 'capture_room',
          args: { room_id: trimmedRoomId, capture_id: 'pending', pattern },
          selection: [selectedId],
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation(
        {
          ...draft,
          args: createCaptureArgs(trimmedRoomId, draft.intent_id, pattern),
        },
        expiresAt,
      )
    },
    [
      intentDependencies,
      stageForConfirmation,
      state.aircraft,
      state.capturePattern,
      state.selection,
      state.sessionId,
      state.enabledIntentNames,
    ],
  )

  /**
   * Drafts a select that must be previewed and confirmed before it is sent; the
   * speech compiler and the target strip use it so nothing leaves on a compile.
   */
  const prepareSelect = useCallback(
    (ids: DroneId[], source: DraftSource, expiresAt?: number): IntentV1 | null => {
      if (!isIntentEnabled(state, 'select')) return null
      const desired = [...new Set(ids)].sort((a, b) => a - b)
      if (desired.length === 0) return null
      const allReady = desired.every(
        (id) => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable,
      )
      if (!allReady) return null
      const draft = createIntent(
        {
          name: 'select',
          args: { ids: desired },
          selection: desired,
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation(draft, expiresAt)
    },
    [intentDependencies, stageForConfirmation, state],
  )

  /**
   * Drafts any control press as a preview that must be confirmed before it is
   * sent, whatever the name's own confirmation rule. The relay-compiled speech
   * path stages every plan step through this or the name-specific prepare
   * functions so nothing leaves on a compile.
   */
  const prepareIntent = useCallback(
    <N extends ConsoleIntentName>(
      request: IntentRequest<N>,
      source: DraftSource = 'console',
      expiresAt?: number,
    ): IntentV1 | null => {
      if (!isIntentEnabled(state, request.name)) return null
      const fleetWide = ['arm', 'land_all', 'estop'].includes(request.name)
      const selection = fleetWide ? [] : request.targets ?? state.selection
      if (!fleetWide && selection.length === 0) return null
      const draft = createIntent(
        {
          name: request.name,
          args: request.args,
          selection,
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation(draft, expiresAt)
    },
    [intentDependencies, stageForConfirmation, state],
  )

  /** Stage the exact relay-minted language draft; no name-specific rewrite is allowed. */
  const prepareVoicePlanStep = useCallback(
    (plan: VoicePlan, step: VoicePlanStep, expiresAt: number): IntentV1 | null => {
      if (
        plan.kind !== 'plan' ||
        plan.plan_digest === null ||
        plan.session !== state.sessionId ||
        plan.roster_version !== state.rosterVersion ||
        state.languageConnection.status !== 'connected' ||
        expiresAt !== plan.expires_at_ms ||
        intentDependencies.now() >= expiresAt ||
        !isIntentEnabled(state, step.name)
      ) {
        return null
      }
      const draft = intentFromVoicePlanStep(plan, step, intentDependencies.now())
      if (draft === null) return null
      const intentCanonical = canonicalVoiceIntent(draft)
      return stageForConfirmation(draft, expiresAt, {
        planDigest: plan.plan_digest,
        correlationId: plan.correlation_id,
        stateEventId: plan.state_event_id,
        session: plan.session,
        stepIndex: step.index,
        intentId: step.intent_id,
        intentCanonical,
        stateCanonical: canonicalLanguageState(state),
      })
    },
    [intentDependencies, stageForConfirmation, state],
  )

  /** Drafts a hold that must be previewed and confirmed before it is sent. */
  const prepareHold = useCallback(
    (source: DraftSource, expiresAt?: number): IntentV1 | null => {
      if (!isIntentEnabled(state, 'hold')) return null
      if (state.selection.length === 0) return null
      const selectionReady = state.selection.every(
        (id) => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable,
      )
      if (!selectionReady) return null
      const draft = createIntent(
        {
          name: 'hold',
          args: {},
          selection: state.selection,
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation(draft, expiresAt)
    },
    [intentDependencies, stageForConfirmation, state],
  )

  const confirmRequest = useCallback(
    (intentId: string): IntentV1 | null => {
      const request = state.requests.find((item) => item.intent.intent_id === intentId)
      if (!request || request.status !== 'pending_confirmation' || confirmedIds.current.has(intentId)) return null
      if (!isIntentEnabled(state, request.intent.name)) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'capability_disabled',
          detail: capabilityBlockedReason(state, request.intent.name) ?? 'The capability was disabled.',
        })
        return null
      }
      if (request.plan?.rosterVersion !== state.rosterVersion) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'stale_roster',
          detail: `Preview used roster version ${request.plan?.rosterVersion}; current roster is ${state.rosterVersion}.`,
        })
        return null
      }
      const expiresAt = request.plan?.expiresAt
      if (expiresAt !== undefined && intentDependencies.now() >= expiresAt) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'confirmation_window_expired',
          detail: 'The confirmation window expired before the operator confirmed. No command was sent.',
        })
        return null
      }
      const voiceBinding = request.plan?.voiceBinding
      if (
        voiceBinding !== undefined &&
        (state.sessionId !== voiceBinding.session ||
          state.languageConnection.status !== 'connected' ||
          request.intent.source !== 'language' ||
          request.intent.intent_id !== voiceBinding.intentId ||
          canonicalVoiceIntent(request.intent) !== voiceBinding.intentCanonical ||
          canonicalLanguageState(state) !== voiceBinding.stateCanonical)
      ) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'language_plan_mismatch',
          detail: 'The staged request no longer matches its exact relay compiler preview. No command was sent.',
        })
        return null
      }
      const selectionMatches = request.intent.selection.length === state.selection.length &&
        request.intent.selection.every((id) => state.selection.includes(id))
      if (!selectionMatches && followsSelection(request.intent.name) && request.intent.name !== 'select') {
        dispatch({ type: 'request_invalidated', intentId, t: intentDependencies.now(),
          reasonCode: 'stale_selection', detail: 'The authoritative selection changed after preview. No command was sent.' })
        return null
      }
      const selectionStillValid =
        selectionRule(request.intent.name) === 'all'
          ? request.intent.selection.every((id) => state.aircraft[id] !== undefined)
          : request.intent.selection.every(
              (id) => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable,
            )
      if (!selectionStillValid) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'stale_selection',
          detail: 'An aircraft in the preview is no longer ready. No command was sent.',
        })
        return null
      }
      if (request.intent.name === 'body_pulse' && (
        !state.armed || state.estop || request.intent.selection.some((id) =>
          !state.aircraft[id]?.adapter_capabilities.includes('body_pulse_v1') ||
          !['airborne', 'hovering'].includes(state.aircraft[id]?.flight_state ?? ''))
      )) {
        dispatch({ type: 'request_invalidated', intentId, t: intentDependencies.now(),
          reasonCode: 'pulse_readiness_changed', detail: 'A selected aircraft is no longer ready for a body pulse. Preview again; nothing was sent.' })
        return null
      }
      const confirmedAt = intentDependencies.now()
      const confirmed = confirmIntent(request.intent, confirmedAt)
      confirmedIds.current.add(intentId)
      sendExistingIntent(confirmed, confirmedAt)
      return confirmed
    },
    [intentDependencies, sendExistingIntent, state],
  )

  const cancelRequest = useCallback(
    (intentId: string) => {
      dispatch({ type: 'request_cancelled', intentId, t: intentDependencies.now() })
    },
    [intentDependencies],
  )

  const issueHold = useCallback(() => {
    if (state.selection.length === 0) return
    issueIntent({ name: 'hold', args: {} })
  }, [issueIntent, state.selection.length])

  const issueNetworkStop = useCallback(
    (source: Extract<IntentSource, 'console' | 'keyboard'>) => {
      const intent = createIntent(
        {
          name: 'estop',
          args: {},
          selection: [],
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      stageIntent(intent)
    },
    [intentDependencies, stageIntent, state.sessionId],
  )

  useEffect(() => {
    const handleKeyboardStop = (event: KeyboardEvent) => {
      if (event.repeat || event.key !== 'Escape' || !event.shiftKey) return
      event.preventDefault()
      issueNetworkStop('keyboard')
    }
    window.addEventListener('keydown', handleKeyboardStop)
    return () => window.removeEventListener('keydown', handleKeyboardStop)
  }, [issueNetworkStop])

  const changeCapturePattern = useCallback(
    (pattern: CapturePattern) => {
      state.requests
        .filter(
          (request) =>
            request.status === 'pending_confirmation' && request.intent.name === 'capture_room',
        )
        .forEach((request) => {
          dispatch({
            type: 'request_invalidated',
            intentId: request.intent.intent_id,
            t: intentDependencies.now(),
            reasonCode: 'capture_pattern_changed',
            detail: 'Capture pattern changed. Build and confirm a new preview.',
          })
        })
      dispatch({ type: 'capture_pattern_changed', pattern })
    },
    [intentDependencies, state.requests],
  )

  /**
   * A change that lands while a preview is pending (the room identifier or an
   * apply-now configuration save, for two) invalidates every unconfirmed
   * preview visibly with the reason stated, exactly as a roster or selection
   * change would.
   */
  const invalidatePending = useCallback(
    (reasonCode: string, detail: string) => {
      const t = intentDependencies.now()
      state.requests
        .filter((request) => request.status === 'pending_confirmation')
        .forEach((request) => {
          dispatch({
            type: 'request_invalidated',
            intentId: request.intent.intent_id,
            t,
            reasonCode,
            detail,
          })
        })
    },
    [intentDependencies, state.requests],
  )

  const retryRequest = useCallback(
    (request: RequestRecord) => {
      if (request.status !== 'failed' && request.status !== 'refused') return
      if (request.intent.source === 'language') return
      const intent = retryIntent(request.intent, intentDependencies)
      if (intent.source === 'webcam' || ['arm', 'body_pulse', 'takeoff', 'land', 'land_all', 'capture_room'].includes(intent.name)) {
        stageForConfirmation({ ...intent, confirm: false })
        return
      }
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      sendNow(intent, t)
    },
    [intentDependencies, sendNow, stageForConfirmation],
  )

  const pendingRequest = useMemo(
    () => state.requests.find((request) => request.status === 'pending_confirmation') ?? null,
    [state.requests],
  )

  return {
    state,
    pendingRequest,
    issueIntent,
    toggleAircraft,
    selectAircraft,
    selectAllReady,
    prepareCapture,
    prepareHold,
    prepareIntent,
    prepareSelect,
    prepareVoicePlanStep,
    confirmRequest,
    cancelRequest,
    issueHold,
    issueNetworkStop,
    changeCapturePattern,
    invalidatePending,
    retryRequest,
    selectFeed: (droneId: DroneId) => dispatch({ type: 'feed_selected', droneId }),
  }
}

function canonicalVoiceIntent(intent: IntentV1): string {
  return JSON.stringify({
    intent_id: intent.intent_id,
    source: intent.source,
    session: intent.session,
    name: intent.name,
    args: canonicalJson(intent.args),
    selection: [...intent.selection],
    mode: intent.mode,
    retry_of: intent.retry_of,
  })
}

function canonicalLanguageState(state: ControlState): string {
  return JSON.stringify({
    sessionId: state.sessionId,
    rosterVersion: state.rosterVersion,
    selection: [...state.selection],
    capabilityProfile: state.capabilityProfile,
    enabledIntentNames: [...state.enabledIntentNames].sort(),
    armed: state.armed,
    estop: state.estop,
    drones: Object.values(state.aircraft)
      .sort((left, right) => left.drone_id - right.drone_id)
      .map((drone) => ({
        droneId: drone.drone_id,
        connectionEpoch: drone.connection_epoch,
        membership: drone.membership,
        selectable: drone.selectable,
        flightState: drone.flight_state,
        cameraPatterns: [...drone.camera_patterns].sort(),
        flightAvailable: drone.adapter_capabilities.includes('flight'),
      })),
  })
}

function canonicalJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJson)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalJson(item)]),
    )
  }
  return value
}

function sendToRelay(
  intent: IntentV1,
  client: RelayClient,
  t: number,
  now: () => number,
  dispatch: Dispatch<Parameters<typeof controlReducer>[1]>,
): void {
  const payload: unknown = intent
  if (!isConsoleIntentV1(payload)) {
    dispatch({
      type: 'request_send_failed',
      intentId: intent.intent_id,
      t,
      detail: 'The console-generated payload failed the local Intent v1 conformance check.',
    })
    return
  }

  void client.sendIntent(intent).catch((error: unknown) => {
    dispatch({
      type: 'request_send_failed',
      intentId: intent.intent_id,
      t: now(),
      detail: error instanceof Error ? error.message : 'Relay send failed for an unknown reason.',
    })
  })
}
