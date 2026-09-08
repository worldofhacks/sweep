import { useEffect, useRef, useState } from 'react'
import { surveyInProgress, surveyLifecycleBlockedReason, surveyStartBlockedReason } from '../../control/survey'
import type { RequestRecord } from '../../control/state'
import { DEVICE_FRESH_MS } from '../../control/observation'
import { downloadSurveyEvidence, surveyCandidateDraft, type SurveyCandidateClient, type SurveyCandidatePreview, type SurveyCandidateReference } from '../../platform/survey-client'
import { exportLocalDraft, MAX_IMAGE_BYTES } from '../map/authoring/files'
import { useSecondTick } from '../live/use-second-tick'
import type { ModuleProps } from '../types'
import './survey.css'

type Props = Pick<ModuleProps, 'controller'> & Partial<Pick<ModuleProps, 'now' | 'services'>>
export function GroundSurveyPane({ controller, now = Date.now, services }: Props) {
  const [areaId, setAreaId] = useState('')
  const state = controller.state
  const request = state.requests.find((item) => item.intent.name === 'survey_area')
  const active = state.requests.some(surveyInProgress)
  useSecondTick(active)
  const transportReason = controller.surveyLifecycleAvailable ? null : 'The console provider has no survey lifecycle transport.'
  const startReason = transportReason ?? surveyStartBlockedReason(state, areaId) ?? (active ? 'Finish the existing recording request before starting another.' : null)
  const run = request?.surveyRun
  const lifecycleReason = transportReason ?? surveyLifecycleBlockedReason(state, request, now(), 'cancel')
  const completeReason = transportReason ?? surveyLifecycleBlockedReason(state, request, now(), 'complete') ?? surveyStartBlockedReason(state, request && 'area_id' in request.intent.args ? request.intent.args.area_id : '')
  const device = run ? state.aircraft[run.deviceId] : undefined
  const scan = device?.connection_epoch === run?.connectionEpoch ? device?.client_observation?.ground?.scan : undefined
  const scanNow = device?.client_observation?.now ?? now()
  const scanCurrent = scan && state.connection.status === 'connected' && scanNow >= scan.t_ingest && scanNow - scan.t_ingest <= DEVICE_FRESH_MS
  const elapsed = run ? now() - run.receivedAt : null
  const reference = candidateReference(state.sessionId, request)
  return <section className="ct-survey" aria-label="Ground survey recording">
    <h3>Survey → Map</h3>
    <p>Record accepted LiDAR observations while the operator controls the robot separately. Starting, completing and cancelling recording never sends a drive command.</p>
    <label>Survey area ID <input value={areaId} maxLength={128} onChange={(event) => setAreaId(event.target.value)} placeholder="Measured area name" /></label>
    <button type="button" disabled={startReason !== null} onClick={() => controller.issueIntent({ name: 'survey_area', args: { area_id: areaId } })}>Preview survey recording</button>
    {startReason && <p>{startReason}</p>}
    {request && <div aria-label="Survey run status">
      <p><strong>{surveyStage(request)}</strong> · {request.intent.intent_id}</p>
      {run && <p>Run {run.runId} · wire ID {run.deviceId} · epoch {run.connectionEpoch}</p>}
      {run && request.status === 'executing' && <p>{elapsed !== null && elapsed >= 0 ? `${Math.floor(elapsed / 1000)} s since the recording acknowledgement` : 'Elapsed time unavailable after a clock change'}.</p>}
      <p>{request.detail}</p>
      {request.surveyUnavailable && <p role="status">{request.surveyUnavailable} The console does not claim that recording stopped.</p>}
      {request.status === 'executing' && <>
        <p>{scanCurrent && scan?.payload.kind === 'range_scan'
          ? `Latest accepted scan for this robot: ${scan.source_id} · ${scan.frame} · ${scan.payload.ranges_m.length} bins · ${Math.max(0, scanNow - scan.t_ingest)} ms old.`
          : 'No fresh accepted scan is visible for this robot.'} Stored scan count and percent complete are not reported by this relay.</p>
        <div className="ct-row">
          <button type="button" disabled={completeReason !== null} onClick={() => controller.sendSurveyLifecycle(request.intent.intent_id, 'complete')}>Complete recording</button>
          <button type="button" disabled={lifecycleReason !== null} onClick={() => controller.sendSurveyLifecycle(request.intent.intent_id, 'cancel')}>Cancel recording</button>
        </div>
        {(lifecycleReason ?? completeReason) && <p role="status">{lifecycleReason ?? completeReason}</p>}
      </>}
      {request.status === 'completed' && !reference && <p>The relay reported completion without a structured candidate receipt. No candidate identity can be inferred from its message.</p>}
    </div>}
    {reference && <CandidatePanel key={`${reference.session}:${reference.candidateId}`} reference={reference} client={services?.surveyCandidates} connected={state.connection.status === 'connected'} />}
    <p>Survey candidates retain their source odometry frame. World registration, measured tag evidence and explicit map approval are separate steps in Map.</p>
  </section>
}
function surveyStage(request: RequestRecord): string {
  if (request.status === 'executing') return request.surveyLifecycle ? 'Recording closure requested; awaiting relay outcome' : 'Relay acknowledged recording'
  if (request.status === 'completed') return request.surveyCandidateId && request.surveyRun ? 'Relay saved an immutable local candidate' : 'Relay reported completion'
  if (request.reasonCode === 'survey_cancelled') return 'Relay confirmed recording cancellation'
  return request.status.replaceAll('_', ' ')
}
function candidateReference(session: string, request: RequestRecord | undefined): SurveyCandidateReference | null {
  if (request?.status !== 'completed' || !request.surveyCandidateId || !request.surveyRun || !('area_id' in request.intent.args)) return null
  return { candidateId: request.surveyCandidateId, session, intentId: request.intent.intent_id, runId: request.surveyRun.runId,
    connectionEpoch: request.surveyRun.connectionEpoch, deviceId: request.surveyRun.deviceId, areaId: request.intent.args.area_id }
}
type CandidateProps = { reference: SurveyCandidateReference; client?: SurveyCandidateClient; connected: boolean }
function CandidatePanel(props: CandidateProps) {
  const referenceKey = JSON.stringify(props.reference)
  const [context, setContext] = useState({ client: props.client, connected: props.connected, referenceKey, generation: 0 })
  if (context.client !== props.client || context.connected !== props.connected || context.referenceKey !== referenceKey) {
    setContext({ client: props.client, connected: props.connected, referenceKey, generation: context.generation + 1 })
  }
  return <CandidateBody key={context.generation} {...props} />
}
function CandidateBody({ reference, client, connected }: CandidateProps) {
  const pending = useRef<AbortController | null>(null)
  const [result, setResult] = useState<{ client: SurveyCandidateClient; candidate?: SurveyCandidatePreview; error?: string; loading?: boolean } | null>(null)
  useEffect(() => () => { pending.current?.abort(); pending.current = null }, [])
  const shown = result?.client === client && connected ? result : null
  const load = () => {
    if (!client || !connected || pending.current) return
    const operation = new AbortController()
    pending.current = operation
    setResult({ client, loading: true })
    void client.load(reference, operation.signal).then((candidate) => {
      if (!operation.signal.aborted) setResult({ client, candidate })
    }, (error: unknown) => {
      if (!operation.signal.aborted) setResult({ client, error: error instanceof Error ? error.message : 'Survey candidate retrieval failed.' })
    }).finally(() => { if (pending.current === operation) pending.current = null })
  }
  return <section aria-label="Survey candidate handoff">
    <h4>Recorded candidate</h4><p>{reference.candidateId}</p>
    <button type="button" disabled={!client || !connected || shown?.loading} onClick={load}>Load recorded candidate</button>
    {(!client || !connected) && <p>Connect a relay platform provider to retrieve this exact candidate.</p>}
    {shown?.loading && <p role="status">Loading and verifying recorded candidate bytes…</p>}
    {shown?.error && <p role="alert">{shown.error}</p>}
    {shown?.candidate && <>
      <p>Local frame: {shown.candidate.frame} · unregistered · no navigation authority.</p>
      <img className="ct-survey-image" src={shown.candidate.image.dataUrl} alt="Recorded local occupancy candidate" />
      <p>{shown.candidate.image.width} × {shown.candidate.image.height} pixels · {shown.candidate.resolutionM} m/pixel · SHA-256 {shown.candidate.image.sha256}</p>
      <div className="ct-row">
        <a href={shown.candidate.image.dataUrl} download={`${reference.candidateId}.png`}>Download occupancy PNG</a>
        <button type="button" onClick={() => downloadSurveyEvidence(shown.candidate!)}>Download candidate evidence</button>
        <button type="button" disabled={shown.candidate.image.bytes > MAX_IMAGE_BYTES} onClick={() => exportLocalDraft(surveyCandidateDraft(shown.candidate!))}>Download editable Map draft</button>
      </div>
      <p>{shown.candidate.image.bytes > MAX_IMAGE_BYTES ? 'This image exceeds the Map editor’s 8 MiB limit. Its original evidence can still be downloaded.' : 'Open Map → Import local draft with the downloaded file. It retains the local frame, blank floor assignment and absent registration. Measure and register the map before validation and approval.'}</p>
    </>}
  </section>
}
