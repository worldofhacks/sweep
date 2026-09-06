import { useEffect, useReducer, useState } from 'react'
import './speech.css'
import { formatDroneId, isTerminalRequest, type ControlState, type RequestRecord } from '../../control/state'
import type { IntentRequest } from '../../control/use-control-console'
import type { CapturePattern, DroneId, IntentV1, VoicePlan, VoicePlanStep } from '../../relay/contract'
import { isValidRoomId } from '../../control/intent'
import { Pane, type PaneTab } from '../../shell/Pane'
import { isReady, sortedAircraft } from '../../shell/derive'
import { humanizeCode, shortId } from '../../shell/format'
import {
  TRY_PHRASES,
  VOICE_FAILS,
  compileUtterance,
  describeCompilerReason,
  describeTranscriptRefusal,
  resolveAmbiguity,
  type AmbiguityOption,
  type CompileContext,
  type CompileOutcome,
} from '../../speech/compiler'
import { UnavailableTranscriptClient, type VoiceOutcome } from '../../voice/client'
import {
  RELAY_MAX_AUDIO_DURATION_MS,
  usePushToTalk,
  type PushToTalkStatus,
} from '../../voice/use-push-to-talk'
import { TargetStrip } from '../gesture/TargetStrip'
import type { ModuleProps } from '../types'

type SpeechPane = 'talk' | 'pipeline'

const PANES: PaneTab[] = [
  { id: 'talk', label: 'Speak or type' },
  { id: 'pipeline', label: 'Compiler pipeline' },
]

const TICK_MS = 1_000
const CAP_SECONDS = RELAY_MAX_AUDIO_DURATION_MS / 1_000
const EXPIRY_URGENT_MS = 10_000
const LANGUAGE_DISABLED =
  'The relay has no transcription endpoint on this console. Type the utterance instead; nothing is emitted from an empty transcript.'
const unavailableTranscriptClient = new UnavailableTranscriptClient(LANGUAGE_DISABLED)

type TranscriptOrigin = 'typed' | 'whisper' | 'template'

interface SeenVoice {
  outcome: VoiceOutcome | null
  status: PushToTalkStatus | null
  detail: string | null
}

/** A relay-compiled plan being previewed and staged one step at a time. */
interface RelayPlanState {
  plan: VoicePlan
  /** Console clock when the plan arrived; the expiry is measured from here. */
  receivedAt: number
  /** Intent IDs of the steps staged so far, in step order. */
  staged: string[]
  stageNote: string | null
}

interface SpeechState {
  utterance: string
  origin: TranscriptOrigin | null
  compiled: CompileOutcome | null
  /** Set when the relay transcribed but refused to compile, so the local fallback ran instead. */
  relayCompilerReason: string | null
  /** Set when the relay compiler returned a plan; the local fallback does not run. */
  relayPlan: RelayPlanState | null
  sttError: string | null
  draftedIntentId: string | null
  draftNote: string | null
  seen: SeenVoice
}

const INITIAL: SpeechState = {
  utterance: '',
  origin: null,
  compiled: null,
  relayCompilerReason: null,
  relayPlan: null,
  sttError: null,
  draftedIntentId: null,
  draftNote: null,
  seen: { outcome: null, status: null, detail: null },
}

/**
 * Speech to intents: hold to talk through the PR #49 recorder and transcript
 * client, or type. When the relay carries the plan compiler its validated plan
 * is previewed step by step and each step is staged through the control flow,
 * so it lands in the same confirmation dock as a button press; the local
 * fallback compiles typed text or a relay transcript without a plan, and the
 * outcome card says which one ran. Nothing is sent from a compile.
 */
export function SpeechModule({ controller, now, roomId, services }: ModuleProps) {
  const [pane, setPane] = useState<SpeechPane>('talk')
  const { state, pendingRequest, issueIntent, prepareCapture, prepareHold, prepareIntent, prepareSelect } =
    controller
  const languageEnabled = services.transcript !== undefined
  const voice = usePushToTalk({
    sessionId: state.sessionId,
    client: services.transcript ?? unavailableTranscriptClient,
    ...services.voice,
  })
  const [speech, setSpeech] = useState<SpeechState>(INITIAL)
  const context = compileContext(state, roomId)
  const relay = speech.relayPlan
  const relayView = relay === null ? null : deriveRelayPlan(relay, state.requests)
  useTicker(voice.isRecording || (relayView !== null && relayView.deadline !== null && !relayView.finished))

  if (
    speech.seen.outcome !== voice.outcome ||
    speech.seen.status !== voice.status ||
    speech.seen.detail !== voice.detail
  ) {
    setSpeech(absorbVoice(speech, voice.outcome, voice.status, voice.detail, context, now()))
  }

  const remainingSeconds =
    voice.startedAt === null
      ? null
      : Math.max(0, Math.ceil((voice.startedAt + voice.maxRecordingMs - now()) / 1_000))
  const captureWord = describeCapture(voice.status, languageEnabled)
  const compiled = speech.compiled
  const blocked = compiled?.status === 'compiled' ? emissionBlockedReason(compiled, state, pendingRequest) : null
  const drafted =
    speech.draftedIntentId === null
      ? null
      : (state.requests.find((request) => request.intent.intent_id === speech.draftedIntentId) ?? null)
  const nextStep = relayView?.next ?? null
  const stageBlocked =
    relay !== null && relayView !== null && nextStep !== null
      ? stageBlockedReason(relay, relayView, nextStep, state, pendingRequest, roomId, now())
      : null

  const setUtterance = (utterance: string, origin: TranscriptOrigin, compile: boolean) =>
    setSpeech((previous) => ({
      ...previous,
      utterance,
      origin,
      compiled: compile ? compileUtterance(utterance, context) : null,
      relayCompilerReason: null,
      relayPlan: null,
      draftNote: null,
    }))

  const pick = (option: AmbiguityOption) =>
    setSpeech((previous) => {
      if (previous.compiled?.status !== 'ambiguous') return previous
      const resolved = resolveAmbiguity(previous.compiled, option, context)
      return {
        ...previous,
        compiled: resolved,
        utterance: resolved === null ? '' : previous.utterance,
        draftNote: null,
      }
    })

  const draft = () => {
    if (!compiled || compiled.status !== 'compiled' || blocked) return
    const intent =
      compiled.intent === 'capture_room'
        ? prepareCapture(compiled.args.room_id, 'console', compiled.args.pattern)
        : compiled.intent === 'hold'
          ? prepareHold('console')
          : prepareSelect(compiled.args.ids, 'console')
    setSpeech((previous) => ({
      ...previous,
      draftedIntentId: intent?.intent_id ?? previous.draftedIntentId,
      draftNote: intent ? null : 'The control flow refused the draft; nothing was emitted.',
    }))
  }

  /**
   * Stages the next relay-compiled step through the same control flow as the
   * buttons: takeoff, land, land_all, capture_room and sweep are parked for
   * confirmation; hold, select and every other name are parked as a preview the
   * operator sends from the dock. The step's targets must still be the
   * authoritative selection; nothing is sent here. The step inherits the plan's
   * deadline, so the dock counts it down and refuses a late confirmation.
   */
  const stageStep = () => {
    if (relay === null || nextStep === null || stageBlocked !== null) return
    const intent = stageThroughController(
      nextStep,
      {
        issueIntent,
        prepareCapture,
        prepareHold,
        prepareIntent,
        prepareSelect,
      },
      relayView?.deadline ?? undefined,
    )
    setSpeech((previous) => {
      if (previous.relayPlan === null) return previous
      return {
        ...previous,
        relayPlan: intent
          ? { ...previous.relayPlan, staged: [...previous.relayPlan.staged, intent.intent_id], stageNote: null }
          : {
              ...previous.relayPlan,
              stageNote: 'The control flow refused to stage this step; nothing was emitted.',
            },
      }
    })
  }

  const startRecording = () => {
    if (!languageEnabled) return
    void voice.start()
  }

  return (
    <Pane
      title="Speech to intents"
      note="An utterance compiles to intents, the arbiter validates, you confirm. Never a command straight to an aircraft."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as SpeechPane)}
      tabsLabel="Speech panes"
    >
      <TargetStrip controller={controller} />
      {pane === 'talk' ? (
        <div data-two="1" className="sp-two">
          <div className="sp-column">
            <button
              type="button"
              className={voice.isRecording ? 'sp-listen is-recording' : 'sp-listen'}
              aria-pressed={voice.isRecording}
              disabled={
                !languageEnabled || voice.status === 'requesting_microphone' || voice.status === 'uploading'
              }
              onPointerDown={startRecording}
              onPointerUp={voice.stop}
              onPointerCancel={voice.stop}
              onPointerLeave={(event) => {
                if (event.buttons !== 0) voice.stop()
              }}
              onKeyDown={(event) => {
                if (!event.repeat && (event.key === ' ' || event.key === 'Enter')) {
                  event.preventDefault()
                  startRecording()
                }
              }}
              onKeyUp={(event) => {
                if (event.key === ' ' || event.key === 'Enter') {
                  event.preventDefault()
                  voice.stop()
                }
              }}
            >
              {listenLabel(voice.status, languageEnabled)}
              {voice.isRecording && remainingSeconds !== null && (
                <span className="sp-listen-sub">{remainingSeconds} s left of {CAP_SECONDS}</span>
              )}
            </button>
            <p className="sp-state">
              <span className={`is-${captureWord.tone}`}>state {captureWord.word}</span>
              <span className="is-cap">cap {CAP_SECONDS} s</span>
            </p>
            {!languageEnabled && (
              <p className="sp-hint" role="status" aria-label="Language state">
                {LANGUAGE_DISABLED}
              </p>
            )}
            {speech.sttError && (
              <p role="alert" className="sp-error">
                {speech.sttError}
              </p>
            )}
            <label className="sp-field">
              Utterance
              <textarea
                value={speech.utterance}
                rows={2}
                placeholder="capture the kitchen with a full panorama"
                onChange={(event) => setUtterance(event.target.value, 'typed', false)}
              />
            </label>
            <button
              type="button"
              className="sp-compile"
              onClick={() => setUtterance(speech.utterance, speech.origin ?? 'typed', true)}
            >
              Compile to intents
            </button>
            <p className="sp-eyebrow">Try one</p>
            <div className="sp-phrases">
              {TRY_PHRASES.map((phrase) => (
                <button
                  key={phrase}
                  type="button"
                  className="sp-phrase"
                  onClick={() => setUtterance(phrase, 'typed', true)}
                >
                  {phrase}
                </button>
              ))}
            </div>
          </div>

          <div className="sp-column">
            {relay && relayView && (
              <RelayPlanCard
                relay={relay}
                view={relayView}
                origin={speech.origin}
                now={now()}
                blocked={stageBlocked}
                onStage={stageStep}
              />
            )}
            {compiled && (
              <div className="sp-result" role="region" aria-label="Compiler result">
                <p className="sp-result-head">
                  <span className="sp-eyebrow is-inline">Compiler result</span>
                  <span className={`sp-result-status is-${compiled.status}`}>{compiled.status}</span>
                </p>
                <p className="sp-result-intent">
                  <span className="is-name">{outcomeIntent(compiled)}</span>
                  <span className="is-args">{compiled.status === 'compiled' ? JSON.stringify(compiled.args) : '{}'}</span>
                </p>
                {compiled.status === 'compiled' && (
                  <p className="sp-result-line">
                    Selection resolves to <span className="mono">{compiled.selection}</span> · confirmation required
                  </p>
                )}
                {compiled.status === 'refused' && (
                  <p className="sp-result-line">
                    reason <span className="mono">{compiled.reason}</span>
                  </p>
                )}
                <p className="sp-result-line">
                  compiled by <span className="mono">local fallback</span>
                  {speech.relayCompilerReason
                    ? ` · relay compiler ${speech.relayCompilerReason}`
                    : ' · the relay has no language service on this console'}
                  {' · transcript '}
                  <span className="mono">{speech.origin ?? 'typed'}</span>
                </p>
                <p className="sp-result-sentence">{compiled.sentence}</p>
                {compiled.status === 'ambiguous' && (
                  <div className="sp-options">
                    {compiled.options.map((option) => (
                      <button key={option} type="button" className="sp-option" onClick={() => pick(option)}>
                        {option}
                      </button>
                    ))}
                  </div>
                )}
                {compiled.status === 'compiled' && blocked && <p className="sp-result-blocked">{blocked}</p>}
                {compiled.status === 'compiled' && (
                  <button type="button" className="sp-emit" disabled={blocked !== null} onClick={draft}>
                    Draft for confirmation
                  </button>
                )}
                {compiled.status === 'compiled' && (
                  <p className="sp-drafted">
                    Drafts with source <code>console</code>; the relay registers no language source yet. Nothing
                    is sent until you confirm in the dock.
                  </p>
                )}
                {speech.draftNote && <p className="sp-result-blocked">{speech.draftNote}</p>}
                {drafted && (
                  <p className="sp-drafted" aria-live="polite">
                    Drafted <code title={drafted.intent.intent_id}>{shortId(drafted.intent.intent_id)}</code> ·{' '}
                    {humanizeCode(drafted.status)}
                    {drafted.status === 'pending_confirmation' ? ' — confirm or cancel it in the dock.' : '.'}
                  </p>
                )}
              </div>
            )}
            <div className="sp-fails-wrap">
              <p className="sp-eyebrow is-inline">States that emit nothing</p>
              {VOICE_FAILS.map(([key, value]) => (
                <p key={key} className="sp-fails">
                  <span className="is-key">{key}</span>
                  <span className="is-value">{value}</span>
                </p>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div>
          {pipelineSteps({
            languageEnabled,
            status: voice.status,
            remainingSeconds,
            utterance: speech.utterance,
            origin: speech.origin,
            compiled,
            relayPlan: relay?.plan ?? null,
            pending: pendingRequest !== null,
          }).map((step) => (
            <div key={step.n} className="sp-step">
              <span aria-hidden="true" className="sp-step-mark">
                {step.n}
              </span>
              <div className="sp-step-body">
                <p className="sp-step-head">
                  <span className="sp-step-title">{step.title}</span>
                  <span className="sp-step-value">{step.value}</span>
                </p>
                <p className="sp-step-note">{step.note}</p>
              </div>
            </div>
          ))}
          <p className="sp-note">
            The compiler is the only place a model touches the request path, and its output is schema-constrained
            to canonical intent names and validated deterministically on the relay before and after the model. When
            the relay has no compiler, the local fallback compiles the same schema at reduced accuracy, and the
            outcome card says which one ran.
          </p>
        </div>
      )}
    </Pane>
  )
}

interface StepView {
  step: VoicePlanStep
  request: RequestRecord | null
  /** waiting: not yet staged; staged: in flight; completed; halted: terminal but not completed. */
  phase: 'waiting' | 'staged' | 'completed' | 'halted'
}

interface RelayPlanView {
  steps: StepView[]
  /** The next step to stage, or null when the plan is finished or halted. */
  next: VoicePlanStep | null
  halted: string | null
  finished: boolean
  /** Console-clock deadline derived from the relay's TTL; null for non-plan kinds. */
  deadline: number | null
}

/** Derives each step's phase from the request records; pure, so it runs during render. */
function deriveRelayPlan(relay: RelayPlanState, requests: RequestRecord[]): RelayPlanView {
  const { plan } = relay
  const deadline =
    plan.expires_at_ms === null ? null : relay.receivedAt + (plan.expires_at_ms - plan.compiled_at_ms)
  const steps: StepView[] = plan.steps.map((step, index) => {
    const intentId = relay.staged[index]
    const request =
      intentId === undefined ? null : (requests.find((item) => item.intent.intent_id === intentId) ?? null)
    if (!request) return { step, request: null, phase: 'waiting' }
    if (request.status === 'completed') return { step, request, phase: 'completed' }
    if (isTerminalRequest(request.status)) return { step, request, phase: 'halted' }
    return { step, request, phase: 'staged' }
  })
  const haltedStep = steps.find((view) => view.phase === 'halted')
  const halted = haltedStep
    ? `Plan halted at step ${haltedStep.step.index + 1} (${humanizeCode(haltedStep.request?.status ?? 'halted').toLowerCase()}${
        haltedStep.request?.reasonCode ? `, ${haltedStep.request.reasonCode}` : ''
      }). Nothing further is staged; say it again to compile a fresh plan.`
    : null
  const inFlight = steps.some((view) => view.phase === 'staged')
  const nextIndex = steps.findIndex((view) => view.phase === 'waiting')
  const next = halted === null && !inFlight && nextIndex >= 0 ? steps[nextIndex].step : null
  const finished = plan.kind !== 'plan' || halted !== null || steps.every((view) => view.phase === 'completed')
  return { steps, next, halted, finished, deadline }
}

function stageBlockedReason(
  relay: RelayPlanState,
  view: RelayPlanView,
  step: VoicePlanStep,
  state: ControlState,
  pending: RequestRecord | null,
  roomId: string,
  now: number,
): string | null {
  if (pending) return 'A plan preview is already pending; confirm or cancel it before staging the next step.'
  if (state.connection.status !== 'connected') {
    return `The console connection is ${state.connection.status}. Nothing can be staged.`
  }
  if (state.estop) return 'The network stop is active. Requests are refused until the relay reports it clear.'
  if (view.deadline !== null && now >= view.deadline) {
    return 'The compiled plan expired; say it again to compile a fresh plan.'
  }
  if (state.rosterVersion !== relay.plan.roster_version) {
    return `The roster changed since the plan compiled (v${relay.plan.roster_version} → v${state.rosterVersion}); say it again.`
  }
  if (step.name === 'estop') return 'estop is never staged from speech. Use the network stop, Shift+Escape, or the physical RC.'
  if (!state.enabledIntentNames.includes(step.name)) {
    return `${step.name} is not enabled by the relay capability profile.`
  }
  const notReady = (id: DroneId) => !isReady(state.aircraft[id])
  if (step.name === 'select') {
    const ids = Array.isArray(step.args.ids) ? (step.args.ids as DroneId[]) : []
    const stale = ids.find(notReady)
    if (stale !== undefined) return `${formatDroneId(stale)} is no longer ready.`
    return null
  }
  if (['arm', 'land_all'].includes(step.name)) return null
  const sameSelection =
    step.selection.length === state.selection.length && step.selection.every((id) => state.selection.includes(id))
  if (!sameSelection) {
    return `The selection changed since the plan compiled (step targets ${
      step.selection.map(formatDroneId).join(', ') || 'none'
    }, selection is ${state.selection.map(formatDroneId).join(', ') || 'empty'}); say it again.`
  }
  const stale = state.selection.find(notReady)
  if (stale !== undefined) return `${formatDroneId(stale)} is not ready or selectable.`
  if (step.name === 'capture_room') {
    const room = typeof step.args.room_id === 'string' ? step.args.room_id : ''
    if (!isValidRoomId(room)) return `Room ${room || roomId || '(none)'} is not a valid room identifier.`
    if (state.selection.length !== 1) return 'Select exactly one ready aircraft for capture_room.'
    const selected = state.aircraft[state.selection[0]]
    const pattern = String(step.args.pattern)
    if (!selected.camera_patterns.includes(pattern)) {
      return `${formatDroneId(selected.drone_id)} does not report ${pattern}; the console will not substitute a pattern.`
    }
  }
  return null
}

type StagingController = Pick<
  ModuleProps['controller'],
  'issueIntent' | 'prepareCapture' | 'prepareHold' | 'prepareIntent' | 'prepareSelect'
>

/**
 * Hands one compiled step to the control flow. Confirmation-gated names go
 * through issueIntent, which parks them pending confirmation exactly like the
 * Control buttons; hold, select and capture_room use their prepare functions;
 * everything else is parked as a preview through prepareIntent. `expiresAt`
 * is the plan's console-clock deadline, carried into the dock preview so a
 * step cannot be confirmed after its plan expired. Never sends.
 */
function stageThroughController(
  step: VoicePlanStep,
  controller: StagingController,
  expiresAt?: number,
): IntentV1 | null {
  switch (step.name) {
    case 'select':
      return controller.prepareSelect(
        Array.isArray(step.args.ids) ? (step.args.ids as DroneId[]) : [],
        'console',
        expiresAt,
      )
    case 'hold':
      return controller.prepareHold('console', expiresAt)
    case 'capture_room':
      return controller.prepareCapture(
        String(step.args.room_id),
        'console',
        step.args.pattern as CapturePattern,
        expiresAt,
      )
    case 'takeoff':
    case 'land':
    case 'land_all':
    case 'sweep':
      return controller.issueIntent(stepRequest(step), expiresAt)
    case 'estop':
      return null
    default:
      return controller.prepareIntent(stepRequest(step), 'console', expiresAt)
  }
}

function stepRequest(step: VoicePlanStep): IntentRequest {
  return {
    name: step.name,
    args: step.args as unknown as IntentRequest['args'],
    targets: [...step.selection],
  }
}

function RelayPlanCard({
  relay,
  view,
  origin,
  now,
  blocked,
  onStage,
}: {
  relay: RelayPlanState
  view: RelayPlanView
  origin: TranscriptOrigin | null
  now: number
  blocked: string | null
  onStage: () => void
}) {
  const { plan } = relay
  const remainingMs = view.deadline === null ? null : Math.max(0, view.deadline - now)
  const reason = plan.kind === 'plan' || plan.kind === 'cancel_pending' ? null : describeCompilerReason(plan.reason, plan.detail)
  const next = view.next
  return (
    <div className="sp-result" role="region" aria-label="Compiled plan">
      <p className="sp-result-head">
        <span className="sp-eyebrow is-inline">Relay plan</span>
        <span className={`sp-result-status is-${planTone(plan.kind)}`}>{plan.kind}</span>
      </p>
      <p className="sp-result-line">
        compiled by <span className="mono">relay compiler</span> · model <span className="mono">{plan.model}</span> ·
        response <span className="mono">{plan.response_source}</span> · transcript{' '}
        <span className="mono">{origin ?? 'whisper'}</span> · state <span className="mono">{shortId(plan.state_event_id)}</span>{' '}
        · roster v{plan.roster_version}
      </p>
      {plan.kind === 'plan' && (
        <>
          <p className="sp-result-sentence">
            The relay compiled {plan.steps.length} step{plan.steps.length === 1 ? '' : 's'} from “{plan.transcript}”.
            Each step is staged into the dock one at a time; nothing is sent until you confirm it there.
          </p>
          {plan.detail && <p className="sp-result-line">{plan.detail}</p>}
          <ol className="sp-plan-steps">
            {view.steps.map(({ step, request, phase }) => (
              <li
                key={step.index}
                className={next?.index === step.index ? 'sp-plan-step is-next' : 'sp-plan-step'}
                aria-label={`Step ${step.index + 1}`}
              >
                <p className="sp-plan-step-head">
                  <span className="is-index">{step.index + 1}</span>
                  <span className="is-name">{step.name}</span>
                  <span className="is-targets">
                    {step.selection.length ? step.selection.map(formatDroneId).join(', ') : 'whole roster'}
                  </span>
                  <span className="is-args">{JSON.stringify(step.args)}</span>
                  {step.confirm_required ? (
                    <span className="is-confirm">confirmation required</span>
                  ) : (
                    <span className="is-send">sends when you press Confirm and send</span>
                  )}
                </p>
                <ul className="sp-plan-notes">
                  {step.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
                <p className={`sp-plan-step-status is-${phase}`} aria-live="polite">
                  {phase === 'waiting' && (next?.index === step.index ? 'next to stage' : 'waiting for the step before it')}
                  {phase !== 'waiting' && request && (
                    <>
                      staged <code title={request.intent.intent_id}>{shortId(request.intent.intent_id)}</code> ·{' '}
                      {humanizeCode(request.status)}
                      {request.status === 'pending_confirmation' ? ' — confirm or cancel it in the dock.' : ''}
                    </>
                  )}
                </p>
              </li>
            ))}
          </ol>
          {remainingMs !== null && (
            <p
              className={
                remainingMs === 0
                  ? 'sp-plan-expiry is-expired'
                  : remainingMs < EXPIRY_URGENT_MS
                    ? 'sp-plan-expiry is-urgent'
                    : 'sp-plan-expiry'
              }
            >
              {remainingMs === 0 ? 'plan expired' : `plan expires in ${Math.ceil(remainingMs / 1_000)} s`}
            </p>
          )}
          {view.halted && <p className="sp-result-blocked">{view.halted}</p>}
          {view.finished && !view.halted && <p className="sp-drafted">Every step reached completed. Nothing further is staged.</p>}
          {next && blocked && <p className="sp-result-blocked">{blocked}</p>}
          {next && (
            <button type="button" className="sp-emit" disabled={blocked !== null} onClick={onStage}>
              Stage step {next.index + 1}: {next.name}
            </button>
          )}
          {relay.stageNote && <p className="sp-result-blocked">{relay.stageNote}</p>}
          <p className="sp-drafted">
            Steps stage with source <code>console</code> and pass the arbiter exactly like a button press. A staged
            step inherits the plan's expiry: the dock counts it down and refuses to confirm past it. The next step is
            offered only after the one before it reaches a terminal state; it is never sent on its own.
          </p>
        </>
      )}
      {plan.kind === 'clarify' && reason && (
        <>
          <p className="sp-result-line">
            reason <span className="mono">{plan.reason}</span>
          </p>
          <p className="sp-result-sentence">{reason.sentence}</p>
          {plan.options.length > 0 && (
            <>
              <p className="sp-result-line">Say one of these:</p>
              <ul className="sp-plan-options" aria-label="Clarification options">
                {plan.options.map((option) => (
                  <li key={option}>{option}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {(plan.kind === 'refuse' || plan.kind === 'unsupported') && reason && (
        <>
          <p className="sp-result-line">
            reason <span className="mono">{plan.reason}</span>
          </p>
          <p className="sp-result-sentence">{reason.sentence}</p>
        </>
      )}
      {plan.kind === 'cancel_pending' && (
        <p className="sp-result-sentence">
          The relay compiler read this as cancelling pending intent{' '}
          <code title={plan.pending_intent_id ?? ''}>{shortId(plan.pending_intent_id ?? '')}</code>. Cancel it from the
          dock or the Requests pane if that is what you meant; nothing was emitted.
        </p>
      )}
    </div>
  )
}

function planTone(kind: VoicePlan['kind']): 'compiled' | 'ambiguous' | 'refused' {
  if (kind === 'plan') return 'compiled'
  if (kind === 'clarify' || kind === 'cancel_pending') return 'ambiguous'
  return 'refused'
}

function compileContext(state: ControlState, roomId: string): CompileContext {
  return {
    roomId: roomId.trim(),
    pattern: state.capturePattern,
    readyIds: sortedAircraft(state.aircraft)
      .filter(isReady)
      .map((drone) => drone.drone_id),
  }
}

/** Folds a recorder or transcript change into the module state; pure, so it runs during render. */
function absorbVoice(
  previous: SpeechState,
  outcome: VoiceOutcome | null,
  status: PushToTalkStatus,
  detail: string | null,
  context: CompileContext,
  now: number,
): SpeechState {
  const seen: SeenVoice = { outcome, status, detail }
  let next: SpeechState = { ...previous, seen }
  if (status === 'requesting_microphone' || status === 'recording') {
    next = { ...next, sttError: null, draftNote: null }
  }
  if (status === 'error') {
    next = { ...next, sttError: detail ?? 'Voice capture failed. Nothing was emitted.' }
  }
  if (outcome !== previous.seen.outcome && outcome !== null) {
    const transcript = outcome.transcript?.trim() ?? ''
    if (outcome.status === 'transcribed' && outcome.plan) {
      // The relay compiled the transcript: preview its plan; the local matcher does not run.
      next = {
        ...next,
        utterance: transcript,
        origin: outcome.source,
        compiled: null,
        relayCompilerReason: null,
        relayPlan: { plan: outcome.plan, receivedAt: now, staged: [], stageNote: null },
        sttError: null,
        draftNote: null,
      }
    } else if (outcome.status === 'transcribed' || (outcome.reason === 'compiler_unavailable' && transcript)) {
      next = {
        ...next,
        utterance: transcript,
        origin: outcome.source,
        compiled: compileUtterance(transcript, context),
        relayCompilerReason: outcome.status === 'refused' ? (outcome.reason ?? 'unavailable') : null,
        relayPlan: null,
        sttError: null,
        draftNote: null,
      }
    } else {
      const refusal = describeTranscriptRefusal(outcome.reason)
      next = {
        ...next,
        utterance: transcript,
        origin: outcome.source,
        compiled: { status: 'refused', reason: refusal.label, sentence: refusal.sentence },
        relayCompilerReason: null,
        relayPlan: null,
        sttError: null,
        draftNote: null,
      }
    }
  }
  return next
}

function emissionBlockedReason(
  compiled: Extract<CompileOutcome, { status: 'compiled' }>,
  state: ControlState,
  pending: ControlState['requests'][number] | null,
): string | null {
  if (pending) return 'A plan preview is already pending; confirm or cancel it before drafting another.'
  if (state.connection.status !== 'connected') {
    return `The console connection is ${state.connection.status}. Nothing can be drafted.`
  }
  if (state.estop) return 'The network stop is active. Requests are refused until the relay reports it clear.'
  const notReady = (id: number) => !isReady(state.aircraft[id])
  if (compiled.intent === 'select') {
    const stale = compiled.args.ids.find(notReady)
    if (stale !== undefined) return `${formatDroneId(stale)} is no longer ready.`
    if (
      compiled.args.ids.length === state.selection.length &&
      compiled.args.ids.every((id) => state.selection.includes(id))
    ) {
      return 'Every ready aircraft is already selected.'
    }
    return null
  }
  if (state.selection.length === 0) return 'Select at least one ready aircraft.'
  const stale = state.selection.find(notReady)
  if (stale !== undefined) return `${formatDroneId(stale)} is not ready or selectable.`
  if (compiled.intent === 'hold') return null
  if (!compiled.args.room_id) return 'Enter a room identifier.'
  if (state.selection.length !== 1) return 'Select exactly one ready aircraft for capture_room.'
  const selected = state.aircraft[state.selection[0]]
  if (!selected.camera_patterns.includes(compiled.args.pattern)) {
    return `${formatDroneId(selected.drone_id)} does not report ${compiled.args.pattern}; the console will not substitute a pattern.`
  }
  return null
}

function outcomeIntent(outcome: CompileOutcome): string {
  if (outcome.status === 'compiled') return outcome.intent
  if (outcome.status === 'ambiguous') return outcome.base
  return outcome.intent ?? '—'
}

function listenLabel(status: PushToTalkStatus, languageEnabled: boolean): string {
  if (!languageEnabled) return 'Language disabled — type below'
  switch (status) {
    case 'requesting_microphone':
      return 'Requesting the microphone'
    case 'recording':
      return 'Listening — release to transcribe'
    case 'uploading':
      return 'Transcribing'
    default:
      return 'Hold to talk, or type below'
  }
}

function describeCapture(
  status: PushToTalkStatus,
  languageEnabled: boolean,
): { word: string; tone: 'idle' | 'listening' | 'transcribed' | 'unavailable' } {
  if (!languageEnabled) return { word: 'language disabled', tone: 'unavailable' }
  switch (status) {
    case 'requesting_microphone':
      return { word: 'requesting microphone', tone: 'idle' }
    case 'recording':
      return { word: 'listening', tone: 'listening' }
    case 'uploading':
      return { word: 'uploading', tone: 'idle' }
    case 'transcribed':
      return { word: 'transcribed', tone: 'transcribed' }
    case 'refused':
      return { word: 'refused', tone: 'unavailable' }
    case 'error':
      return { word: 'failed', tone: 'unavailable' }
    default:
      return { word: 'idle', tone: 'idle' }
  }
}

function pipelineSteps(input: {
  languageEnabled: boolean
  status: PushToTalkStatus
  remainingSeconds: number | null
  utterance: string
  origin: TranscriptOrigin | null
  compiled: CompileOutcome | null
  relayPlan: VoicePlan | null
  pending: boolean
}): Array<{ n: string; title: string; value: string; note: string }> {
  const capture = !input.languageEnabled
    ? 'language disabled'
    : input.status === 'recording'
      ? `listening · ${input.remainingSeconds ?? CAP_SECONDS} s left`
      : input.status === 'uploading'
        ? 'uploading'
        : input.status === 'requesting_microphone'
          ? 'requesting microphone'
          : input.status === 'error'
            ? 'failed'
            : 'idle'
  const compileValue = input.relayPlan
    ? `${input.relayPlan.kind} · relay compiler`
    : input.compiled
      ? `${input.compiled.status} · local fallback`
      : 'waiting'
  const validateValue = input.relayPlan
    ? input.relayPlan.kind === 'plan'
      ? 'relay grounding and validation, then the Intent v1 mirror and the arbiter per step'
      : 'relay grounding refused a plan'
    : input.compiled?.status === 'compiled'
      ? 'Intent v1 mirror, then the relay arbiter'
      : 'nothing to validate'
  return [
    {
      n: '1',
      title: 'Capture',
      value: capture,
      note: `Push-to-talk on the console, capped at ${CAP_SECONDS} seconds. Audio uploads to the relay transcription endpoint when one exists; typed text otherwise.`,
    },
    {
      n: '2',
      title: 'Transcript',
      value: input.utterance ? `ready · ${input.origin ?? 'typed'}` : 'empty',
      note: 'Treated as data, never as instructions. Detection labels and stream names never enter the compiler.',
    },
    {
      n: '3',
      title: 'Compile',
      value: compileValue,
      note: 'Schema-constrained output: canonical intent names and args only. Ambiguity returns options instead of a guess.',
    },
    {
      n: '4',
      title: 'Validate',
      value: validateValue,
      note: 'The relay validates the plan against its authoritative state before and after the model; the console checks each envelope against its Intent v1 mirror before it leaves; safety rules live in the relay arbiter, not the prompt.',
    },
    {
      n: '5',
      title: 'Confirm',
      value: input.pending ? 'pending in the dock' : 'no pending request',
      note: 'One intent at a time, exactly like a console press.',
    },
  ]
}

/** Re-renders once a second only while a recording countdown or a plan expiry is showing. */
function useTicker(active: boolean): void {
  const [, tick] = useReducer((count: number) => count + 1, 0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => tick(), TICK_MS)
    return () => clearInterval(id)
  }, [active])
}
