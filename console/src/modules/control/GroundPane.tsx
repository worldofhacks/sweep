import { useEffect, useState } from 'react'
import { SURVEY_LIFECYCLE_TIMEOUT_MS } from '../../control/use-control-console'
import { formatDroneId } from '../../control/state'
import { isReady } from '../../shell/derive'
import type { ModuleProps } from '../types'
import { gateControl } from './controls'

const FORWARD_PULSE = { linear_mm_s: 80, angular_mrad_s: 0, duration_ms: 250 }
const TURN_LEFT_PULSE = { linear_mm_s: 0, angular_mrad_s: 350, duration_ms: 250 }
const TURN_RIGHT_PULSE = { linear_mm_s: 0, angular_mrad_s: -350, duration_ms: 250 }

export function GroundPane({ controller, now }: Pick<ModuleProps, 'controller' | 'now'>) {
  const { state, issueIntent, sendSurveyLifecycle } = controller
  const [areaId, setAreaId] = useState(1)
  const [, setTick] = useState(0)
  const selectedId = state.selection.length === 1 ? state.selection[0] : null
  const selected = selectedId === null ? undefined : state.aircraft[selectedId]
  const groundSelected = selected?.node_type === 'ground' ? selected : undefined
  const selectedReason = groundSelected && isReady(groundSelected)
    ? null
    : 'Select one ready ground node.'
  const pulseReason = selectedReason ?? gateControl(state, 'ground_velocity').reason
  const surveyReason = selectedReason ?? gateControl(state, 'survey_area').reason
  const activeSurvey = state.requests.find(
    (request) => request.intent.name === 'survey_area' && request.status === 'executing',
  )
  const run = activeSurvey?.surveyRun
  const runGround = activeSurvey === undefined ? undefined : state.aircraft[activeSurvey.intent.selection[0]]
  const currentRun = run !== undefined &&
    runGround?.node_type === 'ground' &&
    runGround.connection_epoch === run.connectionEpoch
  const pending = activeSurvey?.surveyLifecycle
  const pendingTimedOut = pending !== undefined && now() - pending.sentAt >= SURVEY_LIFECYCLE_TIMEOUT_MS
  useEffect(() => {
    if (pending === undefined || pending.error !== undefined) return
    const remaining = SURVEY_LIFECYCLE_TIMEOUT_MS - (now() - pending.sentAt)
    if (remaining <= 0) return
    const timer = window.setTimeout(() => setTick((value) => value + 1), remaining)
    return () => window.clearTimeout(timer)
  }, [now, pending])
  const lifecycleReason =
    activeSurvey === undefined
      ? 'No survey is executing.'
      : run === undefined
        ? 'Awaiting the relay acknowledgement that names this survey run.'
        : !currentRun
          ? 'The ground node reconnected or left the authoritative epoch. This survey cannot be completed here.'
          : state.connection.status !== 'connected'
            ? `The console connection is ${state.connection.status}. Lifecycle requests are not sent.`
            : pending !== undefined && pending.error === undefined && !pendingTimedOut
              ? `Awaiting the terminal ${pending.operation} acknowledgement.`
              : null

  const startSurvey = () => {
    if (surveyReason !== null) return
    issueIntent({ name: 'survey_area', args: { area_id: String(areaId) } })
  }
  const pulse = (args: typeof FORWARD_PULSE) => {
    if (pulseReason !== null) return
    issueIntent({ name: 'ground_velocity', args })
  }

  return (
    <div className="ct-ground" aria-label="Ground controls">
      <div className="ct-ground-status">
        <p className="ct-eyebrow">Ground readiness</p>
        {groundSelected ? (
          <p>
            {formatDroneId(groundSelected.drone_id)} is {isReady(groundSelected)
              ? 'ready'
              : `not ready (${groundSelected.readiness_reasons.join(', ') || groundSelected.membership})`}; pose source{' '}
            <code>{groundSelected.ground_readiness?.source_id ?? 'unreported'}</code>.
          </p>
        ) : (
          <p>Select one ready ground node. The relay has not selected a usable ground target.</p>
        )}
      </div>

      <section className="ct-ground-section" aria-labelledby="ground-pulse-heading">
        <p id="ground-pulse-heading" className="ct-eyebrow">Pilot pulse</p>
        <div className="ct-static-chips" role="group" aria-label="Ground motion pulses">
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(FORWARD_PULSE)}>
            Forward · 80 mm/s · 250 ms
          </button>
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(TURN_LEFT_PULSE)}>
            Turn left · 350 mrad/s · 250 ms
          </button>
          <button type="button" className="ct-static-chip" disabled={pulseReason !== null} title={pulseReason ?? 'Confirmation required before send.'} onClick={() => pulse(TURN_RIGHT_PULSE)}>
            Turn right · 350 mrad/s · 250 ms
          </button>
        </div>
        <p className={`ct-ground-note tone-${pulseReason ? 'warn' : 'muted'}`}>
          {pulseReason ?? 'Each pulse requires confirmation. Keep the local stop within reach.'}
        </p>
      </section>

      <section className="ct-ground-section" aria-labelledby="ground-survey-heading">
        <p id="ground-survey-heading" className="ct-eyebrow">Survey recording</p>
        <label className="ct-ground-area">
          Numeric area ID
          <input
            type="number"
            min={1}
            max={2_147_483_647}
            step={1}
            value={areaId}
            onChange={(event) => {
              const value = Number(event.target.value)
              setAreaId(Number.isSafeInteger(value) && value >= 1 && value <= 2_147_483_647 ? value : 1)
            }}
          />
        </label>
        <button type="button" className="ct-control-btn" disabled={surveyReason !== null} title={surveyReason ?? 'Confirmation required before send.'} onClick={startSurvey}>
          Start survey
          <span className="ct-badge">confirm</span>
        </button>
        <p className={`ct-ground-note tone-${surveyReason ? 'warn' : 'muted'}`}>
          {surveyReason ?? 'Records evidence under this numeric ID; it does not create or edit a map.'}
        </p>
        {activeSurvey && (
          <div className="ct-ground-run" aria-live="polite">
            <p>
              Survey {run ? <><code>{run.runId}</code> · epoch {run.connectionEpoch}</> : 'is executing; run identity unreported'}
            </p>
            {pending?.error && <p className="tone-warn">{pending.error} The survey remains executing until a terminal relay acknowledgement arrives.</p>}
            {pendingTimedOut && !pending?.error && <p className="tone-warn">No terminal acknowledgement arrived within 15 seconds. Confirm the relay connection, then retry.</p>}
            <div className="ct-static-chips" role="group" aria-label="Survey lifecycle">
              <button type="button" className="ct-static-chip" disabled={lifecycleReason !== null} title={lifecycleReason ?? undefined} onClick={() => sendSurveyLifecycle(activeSurvey.intent.intent_id, 'complete')}>
                {pending?.operation === 'complete' && !pendingTimedOut && !pending.error ? 'Awaiting completion' : 'Complete survey'}
              </button>
              <button type="button" className="ct-static-chip" disabled={lifecycleReason !== null} title={lifecycleReason ?? undefined} onClick={() => sendSurveyLifecycle(activeSurvey.intent.intent_id, 'cancel')}>
                {pending?.operation === 'cancel' && !pendingTimedOut && !pending.error ? 'Awaiting cancellation' : 'Cancel survey'}
              </button>
            </div>
            {lifecycleReason && <p className="ct-ground-note tone-warn">{lifecycleReason}</p>}
          </div>
        )}
      </section>
    </div>
  )
}
