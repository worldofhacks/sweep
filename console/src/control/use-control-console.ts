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
}

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
    const unsubscribeKeyboard = clients.keyboard.subscribe((event) => {
      if (event.kind === 'connection') {
        dispatch({ type: 'keyboard_connection_changed', connection: event.connection })
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

    clients.console.start()
    clients.keyboard.start()
    return () => {
      unsubscribeConsole()
      unsubscribeKeyboard()
      clients.console.stop()
      clients.keyboard.stop()
    }
  }, [clients])

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
    (intent: IntentV1, client: RelayClient, t: number) => {
      dispatch({ type: 'request_confirmed', intent, t })
      dispatch({ type: 'request_sent', intentId: intent.intent_id, t })
      sendToRelay(intent, client, t, intentDependencies.now, dispatch)
    },
    [intentDependencies],
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

  const prepareCapture = useCallback(
    (roomId: string) => {
      const selectedId = state.selection[0]
      if (state.selection.length !== 1 || !selectedId) return
      const aircraft = state.aircraft[selectedId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return
      if (!aircraft.camera_patterns.includes(state.capturePattern)) return
      const trimmedRoomId = roomId.trim()
      if (!trimmedRoomId) return

      const draft = createIntent(
        {
          name: 'capture_room',
          args: {},
          selection: [selectedId],
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      const intent: IntentV1 = {
        ...draft,
        args: createCaptureArgs(trimmedRoomId, draft.intent_id, state.capturePattern),
      }
      const t = intentDependencies.now()
      const plan = capturePlanPreview(intent, state.rosterVersion)
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      dispatch({ type: 'request_pending_confirmation', intentId: intent.intent_id, t, plan })
    },
    [intentDependencies, state.aircraft, state.capturePattern, state.rosterVersion, state.selection, state.sessionId],
  )

  const confirmRequest = useCallback(
    (intentId: string) => {
      const request = state.requests.find((item) => item.intent.intent_id === intentId)
      if (!request || request.status !== 'pending_confirmation') return
      if (request.plan?.rosterVersion !== state.rosterVersion) {
        dispatch({
          type: 'request_invalidated',
          intentId,
          t: intentDependencies.now(),
          reasonCode: 'stale_roster',
          detail: `Preview used roster version ${request.plan?.rosterVersion}; current roster is ${state.rosterVersion}.`,
        })
        return
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
        return
      }
      const confirmedAt = intentDependencies.now()
      sendExistingIntent(confirmIntent(request.intent, confirmedAt), clients.console, confirmedAt)
    },
    [
      clients.console,
      intentDependencies,
      sendExistingIntent,
      state.aircraft,
      state.requests,
      state.rosterVersion,
    ],
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
    (source: IntentSource) => {
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

  /**
   * A change that lands while a preview is pending invalidates it visibly with
   * the stated reason, exactly as a roster or selection change would.
   */
  const invalidatePendingRequests = useCallback(
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

  const retryFailedRequest = useCallback(
    (request: RequestRecord) => {
      if (request.status !== 'failed') return
      const retry = retryIntent(request.intent, intentDependencies)
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(retry, t) })

      if (retry.name === 'capture_room') {
        dispatch({
          type: 'request_pending_confirmation',
          intentId: retry.intent_id,
          t,
          plan: capturePlanPreview(retry, state.rosterVersion),
        })
        return
      }

      dispatch({ type: 'request_sent', intentId: retry.intent_id, t })
      const client = retry.source === 'keyboard' ? clients.keyboard : clients.console
      sendToRelay(retry, client, t, intentDependencies.now, dispatch)
    },
    [clients.console, clients.keyboard, intentDependencies, state.rosterVersion],
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
    confirmRequest,
    cancelRequest,
    issueHold,
    issueNetworkStop,
    changeCapturePattern,
    invalidatePendingRequests,
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

function capturePlanPreview(intent: IntentV1, rosterVersion: number): PlanPreview {
  if (intent.name !== 'capture_room' || !('pattern' in intent.args)) {
    throw new Error('Capture preview requires a capture_room intent.')
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
