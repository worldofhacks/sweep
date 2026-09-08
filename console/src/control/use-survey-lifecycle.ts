import { useCallback, useEffect, useLayoutEffect, useRef, type Dispatch } from 'react'
import type { RelayClient } from '../relay/client'
import { isSurveyLifecycleRequest, type SurveyLifecycleRequest } from '../relay/survey'
import { observedControlState } from './observation'
import type { IntentFactoryDependencies } from './intent'
import type { ControlAction, ControlState } from './state'
import { surveyInProgress, surveyLifecycleBlockedReason, surveyStartBlockedReason } from './survey'

export function useSurveyLifecycle(state: ControlState, client: RelayClient,
  dependencies: IntentFactoryDependencies, dispatch: Dispatch<ControlAction>) {
  const current = useRef({ state, client, dependencies })
  useLayoutEffect(() => { current.current = { state, client, dependencies } }, [state, client, dependencies])
  const attempted = useRef(new Set<string>())
  useEffect(() => { attempted.current.clear() }, [state.sessionId])
  useEffect(() => {
    const active = new Set(state.requests.filter(surveyInProgress).map((item) => item.intent.intent_id))
    for (const id of attempted.current) if (!active.has(id)) attempted.current.delete(id)
  }, [state.requests])
  useEffect(() => {
    dispatch({ type: 'survey_provider_changed' })
  }, [client, dispatch])

  return useCallback((intentId: string, operation: SurveyLifecycleRequest['operation']): boolean => {
    const { state: latest, client: activeClient, dependencies: deps } = current.current
    const request = latest.requests.find((item) => item.intent.intent_id === intentId)
    const now = deps.now()
    const observed = observedControlState(latest, now)
    if (surveyLifecycleBlockedReason(observed, request, now, operation) || !request?.surveyRun ||
      !activeClient.sendSurveyLifecycle || attempted.current.has(intentId)) return false
    // Cancellation only closes recording. Completion additionally needs current
    // readiness; the relay independently rechecks the configured scan source.
    if (operation === 'complete' && (!('area_id' in request.intent.args) ||
      surveyStartBlockedReason(observed, String(request.intent.args.area_id), request.intent.selection))) return false
    const frame: SurveyLifecycleRequest = {
      v: 1, t: now, type: 'survey_lifecycle', event_id: deps.nextId(), session: latest.sessionId,
      operation, intent_id: intentId, run_id: request.surveyRun.runId,
      connection_epoch: request.surveyRun.connectionEpoch,
    }
    if (!isSurveyLifecycleRequest(frame)) return false
    // One attempt per run. Transport acceptance is not terminal acknowledgement;
    // an unknown outcome must not produce an automatic or double-click retry.
    attempted.current.add(intentId)
    dispatch({ type: 'survey_lifecycle_sent', intentId, attempt: {
      eventId: frame.event_id, operation, sentAt: now,
    } })
    void activeClient.sendSurveyLifecycle(frame).catch(() => {
      dispatch({ type: 'survey_lifecycle_send_failed', intentId, eventId: frame.event_id,
        error: 'The recording request could not be confirmed by the transport. Its outcome is unknown; no automatic retry was sent.' })
    })
    return true
  }, [dispatch])
}
