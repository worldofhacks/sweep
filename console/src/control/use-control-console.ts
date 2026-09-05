import { useCallback, useEffect, useMemo, useReducer, type Dispatch } from 'react'
import type { RelayClient } from '../relay/client'
import type {
  CapturePattern,
  ConsoleIntentName,
  DroneId,
  IntentArgsByName,
  IntentSource,
  IntentV1,
} from '../relay/contract'
import { isConsoleIntentV1, requiresConfirmation, selectionRule } from '../relay/contract'
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
  controlReducer,
  createInitialControlState,
  createRequestRecord,
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

  /**
   * Records a freshly minted intent, then either parks it for confirmation
   * (with its plan preview) or sends it at once. The intent id never changes.
   */
  const stageIntent = useCallback(
    (intent: IntentV1) => {
      const t = intentDependencies.now()
      dispatch({ type: 'request_created', request: createRequestRecord(intent, t) })
      if (requiresConfirmation(intent.name)) {
        dispatch({
          type: 'request_pending_confirmation',
          intentId: intent.intent_id,
          t,
          plan: buildPlanPreview(intent, state.rosterVersion),
        })
        return
      }
      dispatch({ type: 'request_sent', intentId: intent.intent_id, t })
      const client = intent.source === 'keyboard' ? clients.keyboard : clients.console
      sendToRelay(intent, client, t, intentDependencies.now, dispatch)
    },
    [clients.console, clients.keyboard, intentDependencies, state.rosterVersion],
  )

  const sendExistingIntent = useCallback(
    (intent: IntentV1, client: RelayClient, t: number) => {
      dispatch({ type: 'request_confirmed', intent, t })
      dispatch({ type: 'request_sent', intentId: intent.intent_id, t })
      sendToRelay(intent, client, t, intentDependencies.now, dispatch)
    },
    [intentDependencies],
  )

  const issueIntent = useCallback(
    <N extends ConsoleIntentName>(request: IntentRequest<N>) => {
      const intent = createIntent(
        {
          name: request.name,
          args: request.args,
          selection: request.targets ?? state.selection,
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      stageIntent(intent)
    },
    [intentDependencies, stageIntent, state.selection, state.sessionId],
  )

  const sendSelection = useCallback(
    (desired: DroneId[]) => {
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
      stageIntent(intent)
    },
    [intentDependencies, stageIntent, state.selection, state.sessionId],
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

  const prepareCapture = useCallback(
    (roomId: string) => {
      const selectedId = state.selection[0]
      if (state.selection.length !== 1 || !selectedId) return
      const aircraft = state.aircraft[selectedId]
      if (!aircraft || aircraft.membership !== 'ready' || !aircraft.selectable) return
      if (!aircraft.camera_patterns.includes(state.capturePattern)) return
      const trimmedRoomId = roomId.trim()
      if (!isValidRoomId(trimmedRoomId)) return

      const draft = createIntent(
        {
          name: 'capture_room',
          args: { room_id: trimmedRoomId, capture_id: 'pending', pattern: state.capturePattern },
          selection: [selectedId],
          source: 'console',
          session: state.sessionId,
        },
        intentDependencies,
      )
      stageIntent({
        ...draft,
        args: createCaptureArgs(trimmedRoomId, draft.intent_id, state.capturePattern),
      })
    },
    [intentDependencies, stageIntent, state.aircraft, state.capturePattern, state.selection, state.sessionId],
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
    issueIntent({ name: 'hold', args: {} })
  }, [issueIntent, state.selection.length])

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
   * Retry a failed or refused request as a new intent: new id, retry_of set,
   * same args and selection. Confirmation-gated intents re-enter the preview.
   */
  const retryRequest = useCallback(
    (request: RequestRecord) => {
      if (request.status !== 'failed' && request.status !== 'refused') return
      stageIntent(retryIntent(request.intent, intentDependencies))
    },
    [intentDependencies, stageIntent],
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
    confirmRequest,
    cancelRequest,
    issueHold,
    issueNetworkStop,
    changeCapturePattern,
    retryRequest,
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
