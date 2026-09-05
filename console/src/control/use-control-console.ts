import { useCallback, useEffect, useMemo, useReducer, type Dispatch } from 'react'
import type { RelayClient } from '../relay/client'
import type { CapturePattern, DroneId, IntentSource, IntentV1 } from '../relay/contract'
import { isConsoleIntentV1 } from '../relay/contract'
import {
  browserIntentDependencies,
  confirmIntent,
  createCaptureArgs,
  createIntent,
  retryIntent,
  type IntentFactoryDependencies,
} from './intent'
import {
  controlReducer,
  createInitialControlState,
  createRequestRecord,
  formatDroneId,
  type PlanPreview,
  type RequestRecord,
} from './state'

export interface ControlClients {
  console: RelayClient
  keyboard: RelayClient
  /** Optional gesture producer source; drafts with source webcam are sent through it. */
  webcam?: RelayClient
}

/** Sources that draft previewed requests through the operator control flow. */
export type DraftSource = Extract<IntentSource, 'console' | 'webcam'>

export interface UseControlConsoleOptions {
  sessionId: string
  clients: ControlClients
  intentDependencies?: IntentFactoryDependencies
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

  useEffect(() => {
    const unsubscribeConsole = clients.console.subscribe((event) => {
      if (event.kind === 'connection') {
        dispatch({ type: 'connection_changed', connection: event.connection })
      } else {
        dispatch({ type: 'relay_event', event: event.event })
      }
    })
    const subscribeLifecycleOnly = (
      client: RelayClient,
      connectionType: 'keyboard_connection_changed' | 'webcam_connection_changed',
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
          event.event.type === 'auth.refused'
        ) {
          dispatch({ type: 'relay_event', event: event.event })
        }
      })
    const unsubscribeKeyboard = subscribeLifecycleOnly(clients.keyboard, 'keyboard_connection_changed')
    const unsubscribeWebcam = clients.webcam
      ? subscribeLifecycleOnly(clients.webcam, 'webcam_connection_changed')
      : () => {}

    clients.console.start()
    clients.keyboard.start()
    clients.webcam?.start()
    return () => {
      unsubscribeConsole()
      unsubscribeKeyboard()
      unsubscribeWebcam()
      clients.console.stop()
      clients.keyboard.stop()
      clients.webcam?.stop()
    }
  }, [clients])

  const clientFor = useCallback(
    (source: IntentSource): RelayClient | null => {
      if (source === 'keyboard') return clients.keyboard
      if (source === 'webcam') return clients.webcam ?? null
      return clients.console
    },
    [clients],
  )

  const sendNewIntent = useCallback(
    (intent: IntentV1, client: RelayClient) => {
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      dispatch({ type: 'request_sent', intentId: intent.intent_id, t })
      sendToRelay(intent, client, t, intentDependencies.now, dispatch)
    },
    [intentDependencies],
  )

  const sendExistingIntent = useCallback(
    (intent: IntentV1, t: number) => {
      dispatch({ type: 'request_confirmed', intent, t })
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
    [clientFor, intentDependencies],
  )

  const toggleAircraft = useCallback(
    (droneId: DroneId) => {
      const aircraft = state.aircraft[droneId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return
      const isSelected = state.selection.includes(droneId)
      const desired = isSelected
        ? state.selection.filter((id) => id !== droneId)
        : [...state.selection, droneId].sort((a, b) => a - b)
      if (desired.length === 0) return

      const intent = createIntent(
        {
          name: 'select',
          args: { ids: desired },
          selection: state.selection,
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      sendNewIntent(intent, clients.console)
    },
    [clients.console, intentDependencies, sendNewIntent, state.aircraft, state.selection, state.sessionId],
  )

  const stageForConfirmation = useCallback(
    (intent: IntentV1): IntentV1 => {
      const t = intentDependencies.now()
      const plan = planPreview(intent, state.rosterVersion)
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      dispatch({ type: 'request_pending_confirmation', intentId: intent.intent_id, t, plan })
      return intent
    },
    [intentDependencies, state.rosterVersion],
  )

  /**
   * Drafts a capture_room preview. The pattern defaults to the console's current
   * pattern; the speech compiler passes the pattern the utterance named.
   */
  const prepareCapture = useCallback(
    (
      roomId: string,
      source: DraftSource = 'console',
      pattern: CapturePattern = state.capturePattern,
    ): IntentV1 | null => {
      const selectedId = state.selection[0]
      if (state.selection.length !== 1 || !selectedId) return null
      const aircraft = state.aircraft[selectedId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return null
      if (!aircraft.camera_patterns.includes(pattern)) return null
      const trimmedRoomId = roomId.trim()
      if (!trimmedRoomId) return null

      const draft = createIntent(
        {
          name: 'capture_room',
          args: {},
          selection: [selectedId],
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation({
        ...draft,
        args: createCaptureArgs(trimmedRoomId, draft.intent_id, pattern),
      })
    },
    [
      intentDependencies,
      stageForConfirmation,
      state.aircraft,
      state.capturePattern,
      state.selection,
      state.sessionId,
    ],
  )

  /**
   * Drafts a select that must be previewed and confirmed before it is sent; the
   * speech compiler and the target strip use it so nothing leaves on a compile.
   */
  const prepareSelect = useCallback(
    (ids: DroneId[], source: DraftSource): IntentV1 | null => {
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
          selection: state.selection,
          source,
          session: state.sessionId,
        },
        intentDependencies,
      )
      return stageForConfirmation(draft)
    },
    [intentDependencies, stageForConfirmation, state.aircraft, state.selection, state.sessionId],
  )

  /** Drafts a hold that must be previewed and confirmed before it is sent. */
  const prepareHold = useCallback(
    (source: DraftSource): IntentV1 | null => {
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
      return stageForConfirmation(draft)
    },
    [intentDependencies, stageForConfirmation, state.aircraft, state.selection, state.sessionId],
  )

  const confirmRequest = useCallback(
    (intentId: string): IntentV1 | null => {
      const request = state.requests.find((item) => item.intent.intent_id === intentId)
      if (!request || request.status !== 'pending_confirmation') return null
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
      const selectionStillReady = request.intent.selection.every(
        (id) => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable,
      )
      if (!selectionStillReady) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'stale_selection',
          detail: 'The selected aircraft is no longer ready. No command was sent.',
        })
        return null
      }
      const confirmedAt = intentDependencies.now()
      const confirmed = confirmIntent(request.intent, confirmedAt)
      sendExistingIntent(confirmed, confirmedAt)
      return confirmed
    },
    [intentDependencies, sendExistingIntent, state.aircraft, state.requests, state.rosterVersion],
  )

  const cancelRequest = useCallback(
    (intentId: string) => {
      dispatch({ type: 'request_cancelled', intentId, t: intentDependencies.now() })
    },
    [intentDependencies],
  )

  const issueHold = useCallback(() => {
    if (state.selection.length === 0) return
    const intent = createIntent(
      {
        name: 'hold',
        args: {},
        selection: state.selection,
        source: 'console',
        session: state.sessionId,
      },
      intentDependencies,
    )
    sendNewIntent(intent, clients.console)
  }, [clients.console, intentDependencies, sendNewIntent, state.selection, state.sessionId])

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
      const client = source === 'keyboard' ? clients.keyboard : clients.console
      sendNewIntent(intent, client)
    },
    [clients.console, clients.keyboard, intentDependencies, sendNewIntent, state.sessionId],
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

  const retryFailedRequest = useCallback(
    (request: RequestRecord) => {
      if (request.status !== 'failed') return
      const retry = retryIntent(request.intent, intentDependencies)
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(retry, t) })

      if (retry.name === 'capture_room' || request.plan) {
        dispatch({
          type: 'request_pending_confirmation',
          intentId: retry.intent_id,
          t,
          plan: planPreview(retry, state.rosterVersion),
        })
        return
      }

      dispatch({ type: 'request_sent', intentId: retry.intent_id, t })
      const client = clientFor(retry.source)
      if (!client) {
        dispatch({
          type: 'request_send_failed',
          intentId: retry.intent_id,
          t,
          detail: `No relay connection is bound to source ${retry.source}; the intent was not sent.`,
        })
        return
      }
      sendToRelay(retry, client, t, intentDependencies.now, dispatch)
    },
    [clientFor, intentDependencies, state.rosterVersion],
  )

  const pendingRequest = useMemo(
    () => state.requests.find((request) => request.status === 'pending_confirmation') ?? null,
    [state.requests],
  )

  return {
    state,
    pendingRequest,
    toggleAircraft,
    prepareCapture,
    prepareHold,
    prepareSelect,
    confirmRequest,
    cancelRequest,
    issueHold,
    issueNetworkStop,
    changeCapturePattern,
    retryFailedRequest,
    selectFeed: (droneId: DroneId) => dispatch({ type: 'feed_selected', droneId }),
  }
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

function planPreview(intent: IntentV1, rosterVersion: number): PlanPreview {
  const targets = intent.selection.map(formatDroneId).join(', ')
  if (intent.name === 'hold') {
    return {
      title: `${targets} · hold`,
      rosterVersion,
      steps: [
        'Submit one confirmed hold request for the selected aircraft.',
        `Keep ${targets} at the current pose; no motion is planned.`,
        'Planner and arbiter must revalidate safety before dispatch.',
      ],
    }
  }
  if (intent.name === 'select' && 'ids' in intent.args) {
    const ids = intent.args.ids.map(formatDroneId).join(', ')
    return {
      title: `${ids} · select`,
      rosterVersion,
      steps: [
        'Submit one confirmed select request.',
        `Selection membership becomes ${ids}; no motion is planned.`,
        'The relay reports the authoritative selection in its next state frame.',
      ],
    }
  }
  if (intent.name !== 'capture_room' || !('pattern' in intent.args)) {
    throw new Error('Plan preview requires a capture_room, hold, or select intent.')
  }
  return {
    title: `${formatDroneId(intent.selection[0])} · ${intent.args.pattern}`,
    rosterVersion,
    steps: [
      'Submit one confirmed capture_room outcome request.',
      `Keep ${formatDroneId(intent.selection[0])} at its operator-approved hover pose.`,
      `Request ${intent.args.pattern}; never substitute a different capture pattern.`,
      'Planner and arbiter must revalidate safety and camera readiness before dispatch.',
    ],
  }
}
