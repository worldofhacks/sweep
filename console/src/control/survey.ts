import type { DroneId } from '../relay/contract'
import { isSurveyAreaId, type SurveyLifecycleRequest } from '../relay/survey'
import { isReady } from '../shell/derive'
import { capabilityBlockedReason, type ControlState, type RequestRecord } from './state'

export const SURVEY_LIFECYCLE_TIMEOUT_MS = 15_000

export interface SurveyRun {
  runId: string
  connectionEpoch: number
  deviceId: DroneId
  receivedAt: number
}

export interface SurveyLifecycleAttempt {
  eventId: string
  operation: SurveyLifecycleRequest['operation']
  sentAt: number
  error?: string
}

export function surveyInProgress(request: RequestRecord): boolean {
  return request.intent.name === 'survey_area' &&
    ['draft', 'pending_confirmation', 'sent', 'accepted', 'executing'].includes(request.status)
}

export function surveyStartBlockedReason(state: ControlState, areaId: string, ids = state.selection): string | null {
  if (!isSurveyAreaId(areaId)) return 'Enter an area ID of 1–128 characters.'
  if (state.connection.status !== 'connected') return 'Connect the authenticated console before recording.'
  const capability = capabilityBlockedReason(state, 'survey_area')
  if (capability) return capability
  if (ids.length !== 1 || state.aircraft[ids[0]]?.node_type !== 'ground') return 'Select exactly one ground robot.'
  if (state.estop) return 'The network stop is active.'
  const device = state.aircraft[ids[0]]
  if (!isReady(device) || !device.ground_readiness?.source_id || !device.control_authority) {
    return 'Wait for an accepted current ground pose and a subsequent ready snapshot.'
  }
  return null
}

export function surveyLifecycleBlockedReason(state: ControlState, request: RequestRecord | undefined, now: number, operation: SurveyLifecycleRequest['operation'] = 'complete'): string | null {
  if (!request || request.intent.name !== 'survey_area' || request.status !== 'executing') return 'No acknowledged survey is recording.'
  if (request.surveyControlUnavailable) return request.surveyControlUnavailable
  if (operation === 'complete' && request.surveyUnavailable) return request.surveyUnavailable
  const run = request.surveyRun
  if (!run) return 'Awaiting the relay acknowledgement that names the recording run.'
  if (state.connection.status !== 'connected') return 'The console connection is unavailable.'
  const device = state.aircraft[run.deviceId]
  if (operation === 'complete' && (!device || device.node_type !== 'ground' || device.connection_epoch !== run.connectionEpoch ||
    device.membership === 'disconnected' || device.membership === 'leaving' ||
    device.unit !== request.plan?.groundUnits?.[run.deviceId] ||
    device.ground_readiness?.source_id !== request.plan?.groundSources?.[run.deviceId] ||
    state.selection.length !== 1 || state.selection[0] !== run.deviceId)) {
    return 'The recording target, source or connection changed. Its original run cannot be completed here.'
  }
  const attempt = request.surveyLifecycle
  if (attempt) {
    if (attempt.error) return attempt.error
    return now < attempt.sentAt || now - attempt.sentAt >= SURVEY_LIFECYCLE_TIMEOUT_MS
      ? 'No terminal receipt arrived. The result is unknown; reconnecting or repeating the request does not prove completion.'
      : `Awaiting the relay ${attempt.operation} receipt.`
  }
  return null
}

export function retireSurveys(state: ControlState, detail: string): ControlState {
  return { ...state, requests: state.requests.map((request) => surveyInProgress(request)
    ? { ...request, surveyUnavailable: request.surveyUnavailable ?? detail, surveyControlUnavailable: request.surveyControlUnavailable ?? detail } : request) }
}

export function retainSurveyContext(previous: ControlState, next: ControlState): ControlState {
  if (previous === next || !next.requests.some(surveyInProgress)) return next
  const changed = previous.selection.length !== next.selection.length ||
    previous.selection.some((id) => !next.selection.includes(id))
  return { ...next, requests: next.requests.map((request) => {
    if (!surveyInProgress(request) || !request.plan) return request
    const id = request.intent.selection[0]
    const device = next.aircraft[id]
    const invalid = changed || !device || device.connection_epoch !== request.plan.deviceEpochs?.[id] ||
      device.unit !== request.plan.groundUnits?.[id] ||
      device.ground_readiness?.source_id !== request.plan.groundSources?.[id]
    return invalid ? { ...request, surveyUnavailable: request.surveyUnavailable ??
      'The recording context changed. The prior run remains unavailable; a restored selection cannot revive it.' } : request
  }) }
}
