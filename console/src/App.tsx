import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'
import type { IntentFactoryDependencies } from './control/intent'
import type { ControlClients } from './control/use-control-console'
import { useControlConsole } from './control/use-control-console'
import {
  formatDroneId,
  isTerminalRequest,
  type ControlState,
  type RequestRecord,
} from './control/state'
import type { CapturePattern, DroneId, RelayAircraftState } from './relay/contract'
import { UnavailableTranscriptClient, type TranscriptClient, type VoiceOutcome } from './voice/client'
import { MAX_RECORDING_MS, usePushToTalk, type UsePushToTalkOptions } from './voice/use-push-to-talk'
import { usePushToTalkKey } from './voice/use-push-to-talk-key'

interface AppProps {
  sessionId: string
  clients: ControlClients
  intentDependencies?: IntentFactoryDependencies
  transcriptClient?: TranscriptClient
  voiceOptions?: Pick<UsePushToTalkOptions, 'requestAudio' | 'recorderFactory' | 'nextId'>
}

const MODULES = [
  ['Control / Capture', 'active'],
  ['Live view', 'checkpoint'],
  ['Capture library', 'later'],
  ['World Builder', 'later'],
  ['Connectivity', 'later'],
  ['Configuration', 'later'],
] as const

const unavailableTranscriptClient = new UnavailableTranscriptClient(
  'Voice relay bootstrap is not configured. No audio was sent.',
)

export default function App({ sessionId, clients, intentDependencies, transcriptClient, voiceOptions }: AppProps) {
  const [roomId, setRoomId] = useState('room-01')
  const previewRef = useRef<HTMLElement>(null)
  const {
    state,
    pendingRequest,
    toggleAircraft,
    prepareCapture,
    confirmRequest,
    cancelRequest,
    issueHold,
    issueNetworkStop,
    changeCapturePattern,
    retryFailedRequest,
    selectFeed,
  } = useControlConsole({ sessionId, clients, intentDependencies })
  const voice = usePushToTalk({
    sessionId,
    client: transcriptClient ?? unavailableTranscriptClient,
    ...voiceOptions,
  })

  useEffect(() => {
    if (pendingRequest) previewRef.current?.focus()
  }, [pendingRequest])

  const aircraft = useMemo(
    () => Object.values(state.aircraft).sort((a, b) => a.drone_id - b.drone_id),
    [state.aircraft],
  )
  const activeAircraft = useMemo(() => {
    const selected = aircraft.filter((drone) => state.selection.includes(drone.drone_id))
    const remaining = aircraft.filter(
      (drone) =>
        !state.selection.includes(drone.drone_id) &&
        drone.membership !== 'disconnected' &&
        drone.membership !== 'leaving',
    )
    return [...selected, ...remaining].slice(0, 2)
  }, [aircraft, state.selection])
  const selectedFeed =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)
  const captureBlockedReason = getCaptureBlockedReason(state, roomId)
  const relayUp = state.connection.status === 'connected'
  const readyCount = aircraft.filter((drone) => drone.membership === 'ready' && drone.selectable).length
  const isFixture =
    state.connection.transport === 'fixture' || state.keyboardConnection.transport === 'fixture'
  const voiceEnabled =
    !isFixture &&
    state.connection.status === 'connected' &&
    voice.status !== 'requesting_microphone' &&
    voice.status !== 'uploading'
  usePushToTalkKey({ enabled: voiceEnabled, start: voice.start, stop: voice.stop })

  return (
    <div className="operator-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">SW</span>
          <div>
            <strong>Sweep</strong>
            <span>Operator station</span>
          </div>
        </div>

        <div className="session-strip" aria-label="Session and relay status">
          <StatusPill status={state.connection.status} label={`Console ${state.connection.status}`} />
          <StatusPill
            status={state.keyboardConnection.status}
            label={`Keyboard ${state.keyboardConnection.status}`}
          />
          <span className="mono session-id">{state.sessionId}</span>
          <span className="mono">roster v{state.rosterVersion}</span>
        </div>

        <div className="stop-cluster">
          <span className="shortcut-note">
            Network stop <kbd>Shift</kbd> + <kbd>Esc</kbd>
          </span>
          <button
            type="button"
            className="network-stop"
            onClick={() => issueNetworkStop('console')}
            disabled={state.connection.status !== 'connected'}
            aria-describedby="network-stop-help"
          >
            Network E-stop
          </button>
          <span id="network-stop-help" className="visually-hidden">
            Supplemental network stop. Physical RC pause, return, and landing remain independent.
          </span>
        </div>
      </header>

      {isFixture && (
        <div className="fixture-banner" role="status">
          Development fixture active — no aircraft commands leave this browser.
        </div>
      )}

      <div className="shell-body">
        <aside className="sidebar">
          <nav aria-label="Operator modules">
            <p className="eyebrow">Modules</p>
            <ul className="module-list">
              {MODULES.map(([name, status]) => (
                <li key={name}>
                  <button
                    type="button"
                    className={status === 'active' ? 'module-link is-active' : 'module-link'}
                    aria-current={status === 'active' ? 'page' : undefined}
                    disabled={status !== 'active'}
                  >
                    <span>{name}</span>
                    <span className="module-status">{status}</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          <div className="safety-rail">
            <p className="eyebrow">Independent safety</p>
            <strong>Physical RC remains primary</strong>
            <p>Pause, RTH, land, or take over from each assigned controller.</p>
          </div>
        </aside>

        <main className="workspace">
          <header className="page-heading">
            <div>
              <h1>Control / Capture</h1>
              <p className="page-context">M2.0 checkpoint · indoor room capture · one confirmed request at a time</p>
            </div>
          </header>

          <div className="status-strip" aria-label="Fleet safety state">
            <div className={state.estop || !relayUp ? 'status-tile is-hero is-danger' : 'status-tile is-hero'}>
              <span className="label">Network stop</span>
              <strong>{state.estop ? 'Active' : relayUp ? 'Clear' : 'Unavailable'}</strong>
              <p>
                {state.estop
                  ? 'All aircraft told to stop. Physical RC still governs.'
                  : relayUp
                    ? 'Shift+Esc or the red button sends stop to every aircraft.'
                    : 'No relay link, so no network stop can be sent. Use the physical RC.'}
              </p>
            </div>
            <div className="status-tile">
              <span className="label">Arming</span>
              <strong className={state.armed ? 'is-ok' : undefined}>{state.armed ? 'Armed' : 'Disarmed'}</strong>
              <p>{state.armed ? 'Confirmed intents can dispatch.' : 'Nothing dispatches until armed.'}</p>
            </div>
            <div className="status-tile">
              <span className="label">Aircraft ready</span>
              <strong className="mono">{readyCount} <span>of {aircraft.length}</span></strong>
              <p>{state.selection.length === 0 ? 'None selected.' : `${formatSelection(state.selection)} selected.`}</p>
            </div>
            <div className="status-tile">
              <span className="label">Relay</span>
              <strong className={state.connection.status === 'connected' ? 'is-ok' : 'is-danger'}>
                {humanizeCode(state.connection.status)}
              </strong>
              <p>Keyboard producer {state.keyboardConnection.status} · roster v{state.rosterVersion}</p>
            </div>
          </div>

          {state.connection.status !== 'connected' && (
            <div className="connection-warning" role="alert">
              <div>
                <strong>Network controls unavailable</strong>
                <p>{state.connection.reason ?? 'The relay is not connected.'}</p>
              </div>
              <span>Use physical RC safety controls</span>
            </div>
          )}

          {pendingRequest && (
            <section
              className="panel preview-panel"
              aria-labelledby="preview-title"
              aria-live="polite"
              tabIndex={-1}
              ref={previewRef}
            >
              <PanelHeading
                title="Plan request preview"
                meta="Nothing sent"
                id="preview-title"
              />
              <div className="preview-summary">
                <strong>{pendingRequest.plan?.title}</strong>
                <span className="mono">roster v{pendingRequest.plan?.rosterVersion}</span>
              </div>
              <ol className="preview-steps">
                {pendingRequest.plan?.steps.map((step) => <li key={step}>{step}</li>)}
              </ol>
              <details>
                <summary>Exact Intent v1 draft</summary>
                <pre>{JSON.stringify(pendingRequest.intent, null, 2)}</pre>
              </details>
              <div className="preview-actions">
                <button
                  type="button"
                  className="primary-action compact"
                  onClick={() => confirmRequest(pendingRequest.intent.intent_id)}
                >
                  Confirm and send
                </button>
                <button
                  type="button"
                  className="secondary-action compact"
                  onClick={() => cancelRequest(pendingRequest.intent.intent_id)}
                >
                  Cancel
                </button>
              </div>
            </section>
          )}

          <div className="dashboard-grid">
            <section className="panel registry-panel" aria-labelledby="registry-title">
              <PanelHeading
                title="Aircraft registry"
                meta={`${aircraft.length} known · ${state.selection.length} selected`}
                id="registry-title"
              />
              {aircraft.length === 0 ? (
                <EmptyState
                  title="No aircraft state"
                  detail="Waiting for an authenticated relay snapshot. No local simulator is running."
                />
              ) : (
                <ul className="aircraft-list">
                  {aircraft.map((drone) => {
                    const selected = state.selection.includes(drone.drone_id)
                    const cannotClearLast = selected && state.selection.length === 1
                    const selectable = drone.membership === 'ready' && drone.selectable
                    return (
                      <li className="aircraft-row" key={drone.drone_id}>
                        <button
                          type="button"
                          className={selected ? 'aircraft-selector is-selected' : 'aircraft-selector'}
                          aria-pressed={selected}
                          aria-label={`${formatDroneId(drone.drone_id)} ${humanizeCode(drone.membership)} epoch ${drone.connection_epoch} ${selected ? 'Selected' : 'Select'}`}
                          disabled={!selectable || cannotClearLast}
                          onClick={() => toggleAircraft(drone.drone_id)}
                          title={
                            cannotClearLast
                              ? 'Intent v1 requires at least one aircraft in a select request.'
                              : !selectable
                                ? 'Relay reports this aircraft is not selectable.'
                                : undefined
                          }
                        >
                          <span className={`status-dot status-${drone.membership}`} aria-hidden="true" />
                          <span className="aircraft-identity">
                            <strong className="mono">{formatDroneId(drone.drone_id)}</strong>
                            <span>{humanizeCode(drone.membership)}</span>
                          </span>
                          <span className="aircraft-epoch mono">epoch {drone.connection_epoch}</span>
                          <span className="selection-state">{selected ? 'Selected' : 'Select'}</span>
                        </button>
                        <div className="aircraft-detail">
                          <span className="mono">{drone.adapter_id}</span>
                          <span>{formatReadiness(drone)}</span>
                          <span className="mono">{drone.battery === null ? 'batt —' : `batt ${Math.round(drone.battery * 100)}%`}</span>
                          <button type="button" className="text-button" onClick={() => selectFeed(drone.drone_id)}>
                            View feed
                          </button>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>

            <section className="panel control-panel" aria-labelledby="capture-title">
              <PanelHeading
                title="Room capture"
                meta="Confirmation required"
                id="capture-title"
              />
              <label className="field-label" htmlFor="room-id">Room identifier</label>
              <input
                className="text-field mono"
                id="room-id"
                value={roomId}
                onChange={(event) => setRoomId(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />

              <fieldset className="pattern-fieldset">
                <legend>Capture pattern</legend>
                <div className="pattern-options" role="radiogroup" aria-label="Capture pattern">
                  <PatternButton
                    pattern="pano_360"
                    active={state.capturePattern === 'pano_360'}
                    onSelect={changeCapturePattern}
                    title="Pano 360"
                    detail="Full equirectangular artifact required"
                  />
                  <PatternButton
                    pattern="reconstruct_8"
                    active={state.capturePattern === 'reconstruct_8'}
                    onSelect={changeCapturePattern}
                    title="Reconstruct 8"
                    detail="Incomplete vertical coverage"
                  />
                </div>
              </fieldset>

              <div className={captureBlockedReason ? 'readiness-box is-blocked' : 'readiness-box'}>
                <span className="label">Capture readiness</span>
                <strong>{captureBlockedReason ? 'Blocked' : 'Ready to preview'}</strong>
                <p>{captureBlockedReason ?? 'One ready aircraft and the requested camera pattern are available.'}</p>
              </div>

              <button
                type="button"
                className="primary-action"
                disabled={Boolean(captureBlockedReason)}
                onClick={() => prepareCapture(roomId)}
              >
                Capture room <span>Build preview</span>
              </button>
              <button
                type="button"
                className="secondary-action"
                disabled={state.selection.length === 0 || state.connection.status !== 'connected'}
                onClick={issueHold}
              >
                Hold selected <span>{formatSelection(state.selection)}</span>
              </button>
              <div className="voice-control" aria-live="polite">
                <div className="voice-copy">
                  <span className="label">Voice plan input</span>
                  <strong>
                    {voice.status === 'recording'
                      ? 'Listening'
                      : voice.status === 'uploading'
                        ? 'Transcribing'
                        : voice.status === 'requesting_microphone'
                          ? 'Requesting microphone'
                          : 'Push to talk'}
                  </strong>
                  {voice.status === 'error' && voice.detail ? (
                    <p className="voice-error">{voice.detail}</p>
                  ) : (
                    <p>
                      Hold the button or <kbd>Space</kbd>. Stops after {MAX_RECORDING_MS / 1000}s.
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  className={voice.isRecording ? 'voice-button is-recording' : 'voice-button'}
                  aria-label={voice.isRecording ? 'Release to stop voice recording' : 'Hold to record a voice plan'}
                  aria-pressed={voice.isRecording}
                  disabled={!voiceEnabled}
                  onPointerDown={() => void voice.start()}
                  onPointerUp={voice.stop}
                  onPointerCancel={voice.stop}
                  onPointerLeave={(event) => {
                    if (event.buttons !== 0) voice.stop()
                  }}
                >
                  <span className="voice-glyph" aria-hidden="true">●</span>
                  <span>{voice.isRecording ? 'Release' : 'Hold to talk'}</span>
                </button>
              </div>
              {voice.outcome && <VoiceOutcomeCard outcome={voice.outcome} />}
            </section>

            <section className="panel active-panel" aria-labelledby="active-title">
              <PanelHeading title="Active aircraft" meta="First two" id="active-title" />
              <div className="active-state-grid">
                {[0, 1].map((slot) => {
                  const drone = activeAircraft[slot]
                  return drone ? (
                    <AircraftStateCard
                      drone={drone}
                      selected={state.selection.includes(drone.drone_id)}
                      key={drone.drone_id}
                    />
                  ) : (
                    <div className="drone-state empty-slot" key={`empty-${slot}`}>
                      <span className="mono">SLOT {slot + 1}</span>
                      <strong>Awaiting aircraft</strong>
                      <p>No authoritative state available.</p>
                    </div>
                  )
                })}
              </div>
            </section>

            <section className="panel feed-panel" aria-labelledby="feed-title">
              <PanelHeading
                title="Live feed"
                meta={selectedFeed ? formatDroneId(selectedFeed.drone_id) : 'No feed selected'}
                id="feed-title"
              />
              <FeedSlot drone={selectedFeed} />
            </section>

            <section className="panel request-panel" aria-labelledby="request-title">
              <PanelHeading
                title="Requests"
                meta={`${state.requests.length} this view`}
                id="request-title"
              />
              {state.lastOutcome && (
                <div className={`last-outcome outcome-${state.lastOutcome.kind}`}>
                  <span className="label">Last acknowledgement / refusal</span>
                  <strong>{humanizeCode(state.lastOutcome.status)}</strong>
                  <p>{state.lastOutcome.detail}</p>
                  {state.lastOutcome.reasonCode && <code>{state.lastOutcome.reasonCode}</code>}
                </div>
              )}
              {state.requests.length === 0 ? (
                <EmptyState title="No requests" detail="Button and keyboard intents will appear here." />
              ) : (
                <ol className="request-list">
                  {state.requests.slice(0, 8).map((request) => (
                    <RequestItem
                      key={request.intent.intent_id}
                      request={request}
                      state={state}
                      onRetry={retryFailedRequest}
                    />
                  ))}
                </ol>
              )}
            </section>

            <section className="panel notices-panel" aria-labelledby="notice-title">
              <PanelHeading
                title="Warnings and failures"
                meta={`${state.notices.length} visible`}
                id="notice-title"
              />
              {state.notices.length === 0 ? (
                <EmptyState title="No active notices" detail="Refusals, failures, and invalidations stay visible here." />
              ) : (
                <ol className="notice-list" aria-live="polite">
                  {state.notices.map((notice) => (
                    <li className={`notice-item notice-${notice.level}`} key={notice.id}>
                      <div>
                        <strong>{notice.title}</strong>
                        <span className="mono">{formatTime(notice.t)}</span>
                      </div>
                      <p>{notice.detail}</p>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className="panel departed-panel" aria-labelledby="departed-title">
              <PanelHeading
                title="Departed aircraft"
                meta={`${state.departed.length} records`}
                id="departed-title"
              />
              {state.departed.length === 0 ? (
                <EmptyState title="No departures" detail="Graceful leaves and unexpected loss remain here after rejoin." />
              ) : (
                <ol className="departed-list">
                  {state.departed.map((record, index) => (
                    <li key={`${record.drone.drone_id}-${record.t}-${index}`}>
                      <div>
                        <strong className="mono">{formatDroneId(record.drone.drone_id)}</strong>
                        <span>{humanizeCode(record.action)}</span>
                      </div>
                      <p>{record.detail}</p>
                      <span className="mono">
                        epoch {record.drone.connection_epoch} · {formatTime(record.t)} · {record.reasonCode}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </div>
        </main>
      </div>
    </div>
  )
}

const VOICE_REFUSAL_REASONS: Record<string, string> = {
  session_unavailable: 'The relay has no live session for this console. Nothing was compiled.',
  upload_too_large: 'The recording exceeded the relay upload limit. Nothing was compiled.',
  audio_too_long: 'The recording was longer than the relay accepts. Nothing was compiled.',
  invalid_audio: 'The relay could not decode the recording. Nothing was compiled.',
  invalid_relay_state: 'The relay state could not be grounded for the compiler. Nothing was compiled.',
  transcription_unavailable: 'Whisper transcription failed or is unavailable. Nothing was compiled.',
  invalid_transcript: 'Whisper returned an unusable transcript. Nothing was compiled.',
  compiler_unavailable: 'The transcript compiler is not available on this relay. No plan was produced.',
}

function VoiceOutcomeCard({ outcome }: { outcome: VoiceOutcome }) {
  const refused = outcome.status === 'refused'
  const reason = outcome.reason
  return (
    <section
      className={refused ? 'voice-outcome is-refused' : 'voice-outcome is-transcribed'}
      aria-label="Last voice result"
      data-testid="voice-outcome"
    >
      <div className="voice-outcome-head">
        <span className="label">Last voice result</span>
        <StatusLabel status={outcome.status} />
      </div>
      {outcome.transcript ? (
        <blockquote className="voice-transcript">“{outcome.transcript}”</blockquote>
      ) : (
        <p className="voice-no-transcript">No transcript was produced.</p>
      )}
      {refused ? (
        <div className="voice-refusal">
          <strong>{reason ? (VOICE_REFUSAL_REASONS[reason] ?? humanizeCode(reason)) : 'The relay refused the recording.'}</strong>
          {reason && <code>{reason}</code>}
        </div>
      ) : (
        <p className="voice-plan">
          {outcome.emissions.length === 0
            ? 'No plan was emitted. Nothing was sent to any aircraft.'
            : `${outcome.emissions.length} plan step(s) emitted.`}
        </p>
      )}
      <span className="voice-source mono">source {outcome.source}</span>
    </section>
  )
}

function PanelHeading({ title, meta, id }: { title: string; meta: string; id: string }) {
  return (
    <header className="panel-heading">
      <h2 id={id}>{title}</h2>
      <span className="panel-meta mono">{meta}</span>
    </header>
  )
}

function StatusLabel({ status }: { status: string }) {
  return (
    <span className={`status-label status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {humanizeCode(status)}
    </span>
  )
}

function StatusPill({ status, label }: { status: string; label: string }) {
  return (
    <span className={`status-pill status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {label}
    </span>
  )
}

function PatternButton({ pattern, active, onSelect, title, detail }: {
  pattern: CapturePattern
  active: boolean
  onSelect: (pattern: CapturePattern) => void
  title: string
  detail: string
}) {
  return (
    <button
      type="button"
      className={active ? 'pattern-button is-active' : 'pattern-button'}
      role="radio"
      aria-checked={active}
      onClick={() => onSelect(pattern)}
    >
      <strong>{title}</strong>
      <span>{detail}</span>
    </button>
  )
}

function AircraftStateCard({ drone, selected }: { drone: RelayAircraftState; selected: boolean }) {
  return (
    <article className="drone-state">
      <header>
        <div>
          <strong className="mono">{formatDroneId(drone.drone_id)}</strong>
          {selected && <span className="selected-tag">Selected</span>}
        </div>
        <StatusLabel status={drone.membership} />
      </header>
      <p className="flight-state">{drone.flight_state ?? 'Awaiting telemetry'}</p>
      <Metric label="Battery" value={drone.battery} />
      <Metric label="Link" value={drone.link} />
      <Metric label="Position" value={drone.pos_quality} />
      <dl className="state-facts">
        <div><dt>Epoch</dt><dd className="mono">{drone.connection_epoch}</dd></div>
        <div><dt>Control</dt><dd>{drone.control_authority ? 'Granted' : 'Missing'}</dd></div>
        <div><dt>RC safety</dt><dd>{drone.rc_safety_operator_present ? 'Present' : 'Missing'}</dd></div>
      </dl>
    </article>
  )
}

function Metric({ label, value }: { label: string; value: number | null }) {
  const percentage = value === null ? 0 : Math.round(value * 100)
  return (
    <div className="metric-row">
      <span>{label}</span>
      <div className="meter-track" aria-hidden="true"><span style={{ width: `${percentage}%` }} /></div>
      <strong className="mono">{value === null ? '—' : `${percentage}%`}</strong>
    </div>
  )
}

function FeedSlot({ drone }: { drone: RelayAircraftState | null }) {
  if (!drone) {
    return (
      <div className="feed-empty">
        <span className="reticle" aria-hidden="true"><i /></span>
        <strong>Select “View feed” on an aircraft</strong>
        <p>No stream is selected. Flight controls are unaffected.</p>
      </div>
    )
  }
  if (drone.video?.status === 'live' && drone.video.url) {
    return (
      <video className="live-video" controls muted aria-label={`Live feed for ${formatDroneId(drone.drone_id)}`}>
        <source src={drone.video.url} />
      </video>
    )
  }
  return (
    <div className="feed-empty is-offline">
      <span className="reticle" aria-hidden="true"><i /></span>
      <strong>{formatDroneId(drone.drone_id)} · No video</strong>
      <p>
        Stream {drone.video?.status ?? 'unreported'}
        {drone.video?.last_frame_at ? ` · last frame ${formatTime(drone.video.last_frame_at)}` : ''}
      </p>
    </div>
  )
}

function RequestItem({ request, state, onRetry }: {
  request: RequestRecord
  state: ControlState
  onRetry: (request: RequestRecord) => void
}) {
  const retryReason = getRetryBlockedReason(request, state)
  return (
    <li className="request-item">
      <div className="request-topline">
        <div>
          <strong>{humanizeCode(request.intent.name)}</strong>
          <StatusLabel status={request.status} />
        </div>
        <code title={request.intent.intent_id}>{shortId(request.intent.intent_id)}</code>
      </div>
      <p className="request-target">
        {request.intent.selection.length > 0 ? formatSelection(request.intent.selection) : 'All aircraft'} ·{' '}
        {request.intent.source}
      </p>
      {request.intent.retry_of && <p className="retry-link">Retry of <code>{shortId(request.intent.retry_of)}</code></p>}
      {(request.reasonCode || request.detail) && (
        <div className="request-reason">
          {request.reasonCode && <code>{request.reasonCode}</code>}
          {request.detail && <span>{request.detail}</span>}
        </div>
      )}
      <div className="timestamp-row" aria-label="Request lifecycle timestamps">
        {Object.entries(request.timestamps).map(([status, t]) => (
          <span key={status}>{humanizeCode(status)} <time className="mono">{formatTime(t)}</time></span>
        ))}
      </div>
      {request.status === 'failed' && (
        <button
          type="button"
          className="text-button retry-button"
          disabled={Boolean(retryReason)}
          title={retryReason ?? undefined}
          onClick={() => onRetry(request)}
        >
          Retry as new intent
        </button>
      )}
      {!isTerminalRequest(request.status) && request.status !== 'pending_confirmation' && (
        <span className="in-flight-label">Awaiting terminal result</span>
      )}
    </li>
  )
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><strong>{title}</strong><p>{detail}</p></div>
}

function getCaptureBlockedReason(state: ControlState, roomId: string): string | null {
  if (state.connection.status !== 'connected') return 'Relay is not authenticated. No intent can be sent.'
  if (!roomId.trim()) return 'Enter a room identifier.'
  if (state.selection.length !== 1) return 'Select exactly one ready aircraft.'
  const selected = state.aircraft[state.selection[0]]
  if (!selected || selected.membership !== 'ready' || !selected.selectable) {
    return 'The selected aircraft is not ready or selectable.'
  }
  if (!selected.camera_patterns.includes(state.capturePattern)) {
    return `${formatDroneId(selected.drone_id)} does not report ${state.capturePattern}; the console will not substitute a pattern.`
  }
  return null
}

function getRetryBlockedReason(request: RequestRecord, state: ControlState): string | null {
  if (request.status !== 'failed') return 'Only terminal failed requests can be retried.'
  const connection = request.intent.source === 'keyboard' ? state.keyboardConnection : state.connection
  if (connection.status !== 'connected') return `${request.intent.source} relay source is unavailable.`
  const unavailable = request.intent.selection.find(
    (id) => state.aircraft[id]?.membership !== 'ready' || !state.aircraft[id]?.selectable,
  )
  if (unavailable) return `${formatDroneId(unavailable)} is no longer ready; no substitute will be selected.`
  return null
}

function formatReadiness(drone: RelayAircraftState): string {
  if (drone.selectable && drone.readiness_reasons.length === 0) return 'All readiness gates passed'
  if (drone.readiness_reasons.length === 0) return 'Not selectable; relay supplied no readiness reason'
  return drone.readiness_reasons.map(humanizeCode).join(' · ')
}

function formatSelection(selection: DroneId[]): string {
  return selection.map(formatDroneId).join(', ') || 'None selected'
}

function humanizeCode(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(value)
}
