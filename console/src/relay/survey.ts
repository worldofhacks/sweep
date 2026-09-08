/** Exact console-authenticated lifecycle seam in relay/survey_area.py. */
export interface SurveyRunIdentity {
  run_id: string
  connection_epoch: number
}

export interface SurveyCandidateIdentity extends SurveyRunIdentity {
  candidate_id: string
}

export function isSurveyCandidateId(value: unknown): value is string {
  return typeof value === 'string' && /^candidate-[0-9a-f]{32}$/.test(value)
}

export function isSurveyCandidateIdentity(value: unknown): value is SurveyCandidateIdentity {
  return record(value) && Object.keys(value).length === 3 && isSurveyCandidateId(value.candidate_id) &&
    isSurveyRunIdentity({ run_id: value.run_id, connection_epoch: value.connection_epoch })
}

export interface SurveyLifecycleRequest {
  v: 1
  t: number
  type: 'survey_lifecycle'
  event_id: string
  session: string
  operation: 'complete' | 'cancel'
  intent_id: string
  run_id: string
  connection_epoch: number
}

function text(value: unknown, maximum: number): value is string {
  return typeof value === 'string' && value.length > 0 && Array.from(value).length <= maximum &&
    value === value.trim() && !/[\p{C}\p{Z}]/u.test(value.replaceAll(' ', ''))
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function isSurveyRunIdentity(value: unknown): value is SurveyRunIdentity {
  return record(value) && Object.keys(value).length === 2 && text(value.run_id, 128) &&
    Number.isSafeInteger(value.connection_epoch) && Number(value.connection_epoch) > 0 && Number(value.connection_epoch) <= 2_147_483_647
}

export function isSurveyLifecycleRequest(value: unknown): value is SurveyLifecycleRequest {
  return record(value) && Object.keys(value).length === 9 && value.v === 1 &&
    value.type === 'survey_lifecycle' && Number.isSafeInteger(value.t) && Number(value.t) >= 0 &&
    text(value.event_id, 128) && text(value.session, 512) && text(value.intent_id, 128) &&
    text(value.run_id, 128) && (value.operation === 'complete' || value.operation === 'cancel') &&
    Number.isSafeInteger(value.connection_epoch) && Number(value.connection_epoch) > 0 && Number(value.connection_epoch) <= 2_147_483_647
}

export function isSurveyAreaId(value: unknown): value is string {
  return text(value, 128)
}
