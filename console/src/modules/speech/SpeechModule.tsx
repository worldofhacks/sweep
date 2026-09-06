import { useEffect, useReducer, useState } from 'react'
import './speech.css'
import { formatDroneId, type ControlState } from '../../control/state'
import { Pane, type PaneTab } from '../../shell/Pane'
import { isReady, sortedAircraft } from '../../shell/derive'
import { humanizeCode, shortId } from '../../shell/format'
import {
  TRY_PHRASES,
  VOICE_FAILS,
  compileUtterance,
  describeTranscriptRefusal,
  resolveAmbiguity,
  type AmbiguityOption,
  type CompileContext,
  type CompileOutcome,
} from '../../speech/compiler'
import { UnavailableTranscriptClient, type VoiceOutcome } from '../../voice/client'
import type { LanguageCompilation } from '../../speech/client'
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
const LANGUAGE_DISABLED =
  'The relay has no transcription endpoint on this console. Type the utterance instead; nothing is emitted from an empty transcript.'
const unavailableTranscriptClient = new UnavailableTranscriptClient(LANGUAGE_DISABLED)

type TranscriptOrigin = 'typed' | 'whisper' | 'template'

interface SeenVoice {
  outcome: VoiceOutcome | null
  status: PushToTalkStatus | null
  detail: string | null
}

interface SpeechState {
  utterance: string
  origin: TranscriptOrigin | null
  compiled: CompileOutcome | null
  remote: LanguageCompilation | null
  remoteIndex: number
  stagedIds: string[]
  /** Set when the relay transcribed but refused to compile, so the local fallback ran instead. */
  relayCompilerReason: string | null
  sttError: string | null
  draftedIntentId: string | null
  draftNote: string | null
  seen: SeenVoice
}

const INITIAL: SpeechState = {
  utterance: '',
  origin: null,
  compiled: null,
  remote: null,
  remoteIndex: 0,
  stagedIds: [],
  relayCompilerReason: null,
  sttError: null,
  draftedIntentId: null,
  draftNote: null,
  seen: { outcome: null, status: null, detail: null },
}

/**
 * Speech to intents: hold to talk through the PR #49 recorder and transcript
 * client, or type; use the configured relay compiler or the local fallback.
 * The operator stages each intent for preview and confirms in the dock.
 * Compilation itself sends no aircraft commands.
 */
export function SpeechModule({ controller, now, roomId, services }: ModuleProps) {
  const [pane, setPane] = useState<SpeechPane>('talk')
  const { state, pendingRequest, prepareCapture, prepareHold, prepareSelect, stageProposedIntent } = controller
  const languageEnabled = services.transcript !== undefined
  const compilerEnabled = services.language !== undefined
  const voice = usePushToTalk({
    sessionId: state.sessionId,
    client: services.transcript ?? unavailableTranscriptClient,
    ...services.voice,
  })
  useTicker(voice.isRecording)
  const [speech, setSpeech] = useState<SpeechState>(INITIAL)
  const context = compileContext(state, roomId)

  if (
    speech.seen.outcome !== voice.outcome ||
    speech.seen.status !== voice.status ||
    speech.seen.detail !== voice.detail
  ) {
    setSpeech(absorbVoice(speech, voice.outcome, voice.status, voice.detail, context))
  }

  const remainingSeconds =
    voice.startedAt === null
      ? null
      : Math.max(0, Math.ceil((voice.startedAt + voice.maxRecordingMs - now()) / 1_000))
  const captureWord = describeCapture(voice.status, languageEnabled)
  const compiled = speech.compiled
  const remoteStep = speech.remote?.intents[speech.remoteIndex] ?? null
  const previousStep = speech.remoteIndex === 0 ? null : state.requests.find((request) => request.intent.intent_id === speech.stagedIds[speech.remoteIndex - 1])
  const priorComplete = previousStep?.status === 'completed'
  const priorSelectionMatches = previousStep?.intent.name !== 'select' || (() => { const ids = (previousStep.intent.args as { ids: number[] }).ids; return ids.length === state.selection.length && ids.every((id) => state.selection.includes(id)) })()
  const canStageRemote = remoteStep !== null && pendingRequest === null && state.connection.status === 'connected' && (speech.remoteIndex === 0 || (priorComplete && priorSelectionMatches))
  const queueInvalid =
    state.connection.status !== 'connected' ||
    (speech.remote?.expires_at_ms !== undefined && speech.remote.expires_at_ms !== null && speech.remote.expires_at_ms <= now()) ||
    (previousStep !== null && previousStep !== undefined &&
      !['pending_confirmation', 'sent', 'accepted', 'executing', 'completed'].includes(previousStep.status))
  useEffect(() => {
    if (!queueInvalid || speech.remote === null) return
    const timer = window.setTimeout(() => setSpeech((previous) => ({ ...previous, remote: null, remoteIndex: 0, stagedIds: [], draftNote: 'The relay state changed or the plan expired; remaining proposed steps were discarded.' })), 0)
    return () => window.clearTimeout(timer)
  }, [queueInvalid, speech.remote])
  const blocked = compiled?.status === 'compiled' ? emissionBlockedReason(compiled, state, pendingRequest) : null
  const drafted =
    speech.draftedIntentId === null
      ? null
      : (state.requests.find((request) => request.intent.intent_id === speech.draftedIntentId) ?? null)

  const setUtterance = (utterance: string, origin: TranscriptOrigin, compile: boolean) =>
    setSpeech((previous) => ({
      ...previous,
      utterance,
      origin,
      compiled: compile ? compileUtterance(utterance, context) : null,
      remote: null,
  remoteIndex: 0,
  stagedIds: [],
      relayCompilerReason: null,
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
              onClick={() => {
                if (!compilerEnabled) { setUtterance(speech.utterance, speech.origin ?? 'typed', true); return }
                const text = speech.utterance.trim()
                if (!text) return
                const correlation = `text-${crypto.randomUUID()}`
                void services.language!.compile(text, correlation).then((remote) => setSpeech((previous) => ({ ...previous, remote, remoteIndex: 0, stagedIds: [], compiled: null, utterance: text, origin: 'typed', draftNote: null }))).catch((error: unknown) => setSpeech((previous) => ({ ...previous, remote: null, compiled: { status: 'refused', reason: 'compiler_unavailable', sentence: error instanceof Error ? error.message : 'Language compilation failed. Nothing was emitted.' } })))
              }}
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
            {speech.remote && (
              <div className="sp-result" role="region" aria-label="Compiler result">
                <p className="sp-result-head"><span className="sp-eyebrow is-inline">Relay compilation</span><span className={`sp-result-status is-${speech.remote.kind}`}>{speech.remote.kind}</span></p>
                <p className="sp-result-line">compiled by relay · source <span className="mono">{speech.remote.source}</span></p>
                {speech.remote.detail && <p className="sp-result-sentence">{speech.remote.detail}</p>}
                {speech.remote.reason && <p className="sp-result-line">reason <span className="mono">{speech.remote.reason}</span></p>}
                {speech.remote.intents.map((intent, index) => <p className="sp-result-intent" key={`${intent.name}-${index}`}><span className="is-name">{index + 1}. {intent.name}</span><span className="is-args">{JSON.stringify(intent.args)}</span></p>)}
                {speech.remote.kind === 'plan' && <p className="sp-drafted">Stage one step at a time. Each confirmation waits for the relay lifecycle and current state before the next step. Navigation requests a server preview before confirmation.</p>}
                {speech.remote.kind === 'plan' && remoteStep && <button type="button" className="sp-emit" disabled={!canStageRemote} onClick={() => { const staged = stageProposedIntent({ name: remoteStep.name as never, args: remoteStep.args as never, selection: remoteStep.selection }); setSpeech((previous) => staged ? ({ ...previous, stagedIds: [...previous.stagedIds, staged.intent_id], remoteIndex: previous.remoteIndex + 1, draftNote: `Step ${previous.remoteIndex + 1} is in the confirmation dock. Confirm it, then wait for the authoritative completion before staging the next step.` }) : ({ ...previous, remote: null, remoteIndex: 0, stagedIds: [], draftNote: 'The current authoritative state refused this proposed step; remaining steps were discarded.' })) }}>Stage step {speech.remoteIndex + 1} of {speech.remote.intents.length}</button>}
                {speech.remote.kind === 'plan' && previousStep && !priorComplete && <p className="sp-result-blocked">Wait for the relay to report the preceding step completed before staging the next step.</p>}
                {speech.remote.kind === 'plan' && previousStep?.intent.name === 'select' && priorComplete && !priorSelectionMatches && <p className="sp-result-blocked">The authoritative selection differs from the proposed selection; remaining steps were discarded.</p>}
              </div>
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
                    : !compilerEnabled ? ' · the relay has no language service on this console' : ''}
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
            compilerEnabled,
            status: voice.status,
            remainingSeconds,
            utterance: speech.utterance,
            origin: speech.origin,
            compiled,
            remote: speech.remote,
            remoteIndex: speech.remoteIndex,
            stagedIds: speech.stagedIds,
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
            {compilerEnabled
              ? 'The configured relay compiler returns intents grounded in the current fleet state. The result identifies the compiler source. Review and stage one step at a time; commands are sent only after confirmation.'
              : 'The compiler is the only place a model would touch the request path, and its output is schema-constrained to canonical intent names. The relay has no language service on this console yet, so the local fallback compiles the same schema at reduced accuracy, and the outcome card says which one ran.'}
          </p>
        </div>
      )}
    </Pane>
  )
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
    if (outcome.status === 'transcribed' || (outcome.reason === 'compiler_unavailable' && transcript)) {
      next = {
        ...next,
        utterance: transcript,
        origin: outcome.source,
        compiled: outcome.compilation ? null : compileUtterance(transcript, context),
        remote: outcome.compilation ?? null,
        remoteIndex: 0,
        stagedIds: [],
        relayCompilerReason: outcome.status === 'refused' ? (outcome.reason ?? 'unavailable') : null,
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
  compilerEnabled: boolean
  status: PushToTalkStatus
  remainingSeconds: number | null
  utterance: string
  origin: TranscriptOrigin | null
  compiled: CompileOutcome | null
  remote: LanguageCompilation | null
  remoteIndex: number
  stagedIds: string[]
  pending: boolean
}): Array<{ n: string; title: string; value: string; note: string }> {
  const capture = !input.languageEnabled
    ? input.compilerEnabled ? 'microphone unavailable · typed input ready' : 'language disabled'
    : input.status === 'recording'
      ? `listening · ${input.remainingSeconds ?? CAP_SECONDS} s left`
      : input.status === 'uploading'
        ? 'uploading'
        : input.status === 'requesting_microphone'
          ? 'requesting microphone'
          : input.status === 'error'
            ? 'failed'
            : 'idle'
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
      value: input.remote
        ? `${input.remote.kind} · relay · ${input.remote.source}`
        : input.compiled ? `${input.compiled.status} · local fallback` : 'waiting',
      note: 'Schema-constrained output: canonical intent names and args only. Ambiguity returns options instead of a guess.',
    },
    {
      n: '4',
      title: 'Validate',
      value: input.remote?.kind === 'plan' || input.compiled?.status === 'compiled'
        ? 'Intent v1 mirror, then the relay arbiter' : 'nothing to validate',
      note: 'The console checks the envelope against its Intent v1 mirror before it leaves; safety rules live in the relay arbiter, not the prompt.',
    },
    {
      n: '5',
      title: 'Confirm',
      value: input.remote?.kind === 'plan'
        ? `${input.remoteIndex} of ${input.remote.intents.length} steps staged · ${input.pending ? 'pending in the dock' : 'no pending request'}`
        : input.pending ? 'pending in the dock' : 'no pending request',
      note: 'One intent at a time, exactly like a console press.',
    },
  ]
}

/** Re-renders once a second only while the recording countdown is showing. */
function useTicker(active: boolean): void {
  const [, tick] = useReducer((count: number) => count + 1, 0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => tick(), TICK_MS)
    return () => clearInterval(id)
  }, [active])
}
