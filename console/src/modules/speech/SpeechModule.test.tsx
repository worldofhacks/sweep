import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from '../../App'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { C1_BASIC_CONTROL_INTENTS, type VoicePlan, type VoicePlanStep } from '../../relay/contract'
import type { TranscriptClient, TranscriptRequest, VoiceOutcome } from '../../voice/client'
import type { RecorderFactory } from '../../voice/use-push-to-talk'
import type { VoiceDependencies } from '../types'

const session = 'speech-module-session'
const T0 = 1_756_700_000_000

function step(index: number, name: VoicePlanStep['name'], selection: number[], args: Record<string, unknown> = {}): VoicePlanStep {
  return {
    index,
    intent_id: `voice-${index}-${name}`,
    name,
    args,
    selection,
    mode: 'indoor',
    confirm_required: ['takeoff', 'land', 'land_all', 'capture_room', 'sweep'].includes(name),
    notes: [`Targets ${selection.map((id) => `D-0${id}`).join(', ') || 'the roster'} (the current selection).`],
  }
}

function relayPlan(overrides: Partial<VoicePlan>): VoicePlan {
  return {
    v: 1,
    kind: 'plan',
    transcript: 'Take off.',
    reason: null,
    detail: null,
    options: [],
    steps: [step(0, 'takeoff', [1])],
    compiled_at_ms: T0,
    expires_at_ms: T0 + 30_000,
    state_event_id: 'server-event-12',
    roster_version: 7,
    session,
    correlation_id: 'voice-1',
    plan_digest: 'b'.repeat(64),
    model: 'claude-sonnet-5',
    prompt_schema_version: 'intent-v1-compiler-8',
    response_source: 'anthropic',
    pending_intent_id: null,
    ...overrides,
  }
}

class FakeRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: BlobEvent) => void) | null = null
  onstop: (() => void) | null = null
  onerror: ((event: ErrorEvent) => void) | null = null
  start = vi.fn(() => {
    this.state = 'recording'
  })

  stop = vi.fn(() => {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['recording'], { type: this.mimeType }) } as BlobEvent)
    this.onstop?.()
  })
}

/** A transcript client that answers each upload from a queue of outcomes or failures. */
class QueuedTranscriptClient implements TranscriptClient {
  readonly requests: TranscriptRequest[] = []
  private readonly queue: Array<VoiceOutcome | Error> = []

  answer(next: VoiceOutcome | Error): void {
    this.queue.push(next)
  }

  async transcribe(request: TranscriptRequest): Promise<VoiceOutcome> {
    this.requests.push(request)
    const next = this.queue.shift()
    if (!next) throw new Error('No transcript was queued.')
    if (next instanceof Error) throw next
    const plan = next.plan
      ? { ...next.plan, session: request.sessionId, correlation_id: request.correlationId }
      : next.plan
    return { ...next, session: request.sessionId, correlation_id: request.correlationId, plan }
  }
}

function outcome(overrides: Partial<VoiceOutcome>): VoiceOutcome {
  return {
    v: 1,
    type: 'voice_outcome',
    session,
    correlation_id: 'voice-1',
    status: 'transcribed',
    source: 'whisper',
    reason: null,
    transcript: null,
    emissions: [],
    ...overrides,
  }
}

function mount(options: { transcript?: TranscriptClient; requestAudio?: () => Promise<MediaStream> } = {}) {
  let current = T0
  const now = () => current
  let sequence = 0
  const clients = {
    console: new FixtureRelayClient(session, now, 'console'),
    keyboard: new FixtureRelayClient(session, now, 'keyboard'),
    language: new FixtureRelayClient(session, now, 'language'),
  }
  const recorder = new FakeRecorder()
  const stopTrack = vi.fn()
  const voice: VoiceDependencies = {
    requestAudio:
      options.requestAudio ??
      (async () => ({ getTracks: () => [{ stop: stopTrack }] }) as unknown as MediaStream),
    recorderFactory: (() => recorder) as RecorderFactory,
    nextId: () => 'voice-1',
    now,
  }
  const element = () => (
    <App
      sessionId={session}
      clients={clients}
      intentDependencies={{ now, nextId: () => `speech-intent-${++sequence}` }}
      initialModule="speech"
      services={{ transcript: options.transcript, voice }}
    />
  )
  const view = render(element())
  return {
    clients,
    recorder,
    stopTrack,
    advance: (ms: number) => {
      current += ms
      view.rerender(element())
    },
    /** Moves the clock without a re-render: the gap between two one-second ticks. */
    drift: (ms: number) => {
      current += ms
    },
  }
}

const listenButton = () => screen.getByRole('button', { name: /Hold to talk|Listening|Language disabled|Transcribing/ })
const result = () => screen.getByRole('region', { name: 'Compiler result' })
const planCard = () => screen.getByRole('region', { name: 'Compiled plan' })
const user = () => userEvent.setup()

async function record() {
  fireEvent.pointerDown(listenButton())
  await act(async () => {})
  fireEvent.pointerUp(listenButton())
}

/** The autonomy lifecycle event the relay emits when a plan reaches its terminal state. */
function completed(intentId: string, t: number, sequence: number) {
  return {
    v: 1 as const,
    t,
    event_id: `autonomy-${sequence}`,
    type: 'acknowledgement' as const,
    session,
    intent_id: intentId,
    command_id: null,
    status: 'completed' as const,
    source: 'autonomy',
    drone_id: null,
    connection_epoch: null,
    reason: null,
    detail: 'Completed.',
    roster_version: 7,
  }
}

function authoritativeState(selection: number[], t: number, sequence: number) {
  return {
    v: 1 as const,
    t,
    event_id: `authoritative-state-${sequence}`,
    type: 'state' as const,
    session,
    roster_version: 7,
    state_sequence: sequence,
    armed: true,
    estop: false,
    selection,
    formation: 'line' as const,
    spacing: 1.2,
    mode: 'indoor',
    capability_profile: 'c1_basic_control',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    pending: null,
    accepted_plan: null,
    drones: fixtureAircraft(t),
  }
}

async function compileTyped(u: ReturnType<typeof userEvent.setup>, text: string) {
  const field = screen.getByRole('textbox', { name: 'Utterance' })
  await u.clear(field)
  await u.type(field, text)
  await u.click(screen.getByRole('button', { name: 'Compile to intents' }))
}

describe('Speech module', () => {
  test('language disabled: without a transcription endpoint the hold button is off and typed text still compiles', async () => {
    const { clients } = mount()
    const u = user()
    await screen.findByText(/Development fixture active/i)

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Speech to intents')
    expect(listenButton()).toHaveTextContent('Language disabled — type below')
    expect(listenButton()).toBeDisabled()
    expect(screen.getByText('state language disabled')).toBeInTheDocument()
    expect(screen.getByText('cap 30 s')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Language state' })).toHaveTextContent(
      'The relay has no transcription endpoint on this console. Type the utterance instead; nothing is emitted from an empty transcript.',
    )
    expect(screen.queryByRole('region', { name: 'Compiler result' })).not.toBeInTheDocument()
    const fails = screen.getByText('States that emit nothing').parentElement as HTMLElement
    expect(within(fails).getByText('language disabled')).toBeInTheDocument()
    expect(within(fails).getAllByText(/nothing was emitted/i)).toHaveLength(7)

    await compileTyped(u, 'hold position')
    expect(result()).toHaveTextContent('compiled')
    expect(result()).toHaveTextContent('hold')
    expect(result()).toHaveTextContent('Selection resolves to the selection · confirmation required')
    expect(result()).toHaveTextContent('compiled by local fallback · the relay has no language service on this console · transcript typed')
    expect(result()).toHaveTextContent('Each selected aircraft hovers at its current pose.')
    expect(clients.console.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    const tabs = within(screen.getByRole('group', { name: 'Speech panes' }))
    await u.click(tabs.getByRole('button', { name: 'Compiler pipeline' }))
    expect(screen.getByText('language disabled', { selector: '.sp-step-value' })).toBeInTheDocument()
    expect(screen.getByText('ready · typed')).toBeInTheDocument()
    expect(screen.getByText('compiled · local fallback')).toBeInTheDocument()
    expect(screen.getByText('Intent v1 mirror, then the relay arbiter')).toBeInTheDocument()
    expect(screen.getByText('no pending request')).toBeInTheDocument()
    expect(screen.getAllByText(/^[1-5]$/)).toHaveLength(5)
  })

  test('the confirm rule: a compiled utterance drafts a preview and nothing is sent until the dock confirms it', async () => {
    const { clients } = mount()
    const u = user()
    await screen.findByText(/Development fixture active/i)

    await u.click(screen.getByRole('button', { name: 'capture the kitchen with a full panorama' }))
    expect(result()).toHaveTextContent('capture_room')
    expect(result()).toHaveTextContent('{"room_id":"kitchen-01","pattern":"pano_360"}')
    expect(result()).toHaveTextContent('Selection resolves to exactly one · confirmation required')
    expect(result()).toHaveTextContent('Resolved room kitchen-01 and pattern pano_360.')
    expect(clients.console.sent).toHaveLength(0)

    await u.click(screen.getByRole('button', { name: 'Draft for confirmation' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Capture room')
    expect(dock).toHaveTextContent('source console')
    expect(within(dock).getByText(/"room_id": "kitchen-01"/)).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
    expect(result()).toHaveTextContent('Drafted speech-i…nt-1 · Pending confirmation — confirm or cancel it in the dock.')
    expect(screen.getByRole('button', { name: 'Draft for confirmation' })).toBeDisabled()
    expect(result()).toHaveTextContent('A plan preview is already pending; confirm or cancel it before drafting another.')

    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      intent_id: 'speech-intent-1',
      name: 'capture_room',
      source: 'console',
      selection: [1],
      confirm: true,
      args: { room_id: 'kitchen-01', capture_id: 'capture-speech-intent-1', pattern: 'pano_360' },
    })
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    await waitFor(() => expect(result()).toHaveTextContent('Drafted speech-i…nt-1 · Accepted.'))
    expect(screen.getByRole('button', { name: 'Draft for confirmation' })).toBeEnabled()
  })

  test('ambiguous utterances return options; a pick resolves the target and cancel clears', async () => {
    const { clients } = mount()
    const u = user()
    await screen.findByText(/Development fixture active/i)

    await u.click(screen.getByRole('button', { name: 'freeze that one' }))
    expect(result()).toHaveTextContent('ambiguous')
    expect(result()).toHaveTextContent('The compiler could not resolve the target. Pick one or cancel; nothing was emitted.')
    expect(screen.queryByRole('button', { name: 'Draft for confirmation' })).not.toBeInTheDocument()

    await u.click(within(result()).getByRole('button', { name: 'every ready aircraft' }))
    expect(result()).toHaveTextContent('select')
    expect(result()).toHaveTextContent('{"ids":[1,2,4]}')
    await u.click(screen.getByRole('button', { name: 'Draft for confirmation' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText('select', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
    await u.click(within(dock).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(result()).toHaveTextContent('Cancelled.')

    await u.click(screen.getByRole('button', { name: 'freeze that one' }))
    await u.click(within(result()).getByRole('button', { name: 'the selected aircraft' }))
    expect(result()).toHaveTextContent('hold')
    expect(result()).toHaveTextContent('Resolved from your pick')

    await u.click(screen.getByRole('button', { name: 'freeze that one' }))
    await u.click(within(result()).getByRole('button', { name: 'cancel' }))
    expect(screen.queryByRole('region', { name: 'Compiler result' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Utterance' })).toHaveValue('')
    expect(clients.console.sent).toHaveLength(0)
  })

  test('refused utterances name the reason and never offer a draft', async () => {
    const { clients } = mount()
    const u = user()
    await screen.findByText(/Development fixture active/i)

    await u.click(screen.getByRole('button', { name: 'emergency stop' }))
    expect(result()).toHaveTextContent('refused')
    expect(result()).toHaveTextContent('estop')
    expect(result()).toHaveTextContent('reason not_voice_emittable')
    expect(screen.queryByRole('button', { name: 'Draft for confirmation' })).not.toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: 'take off and hold' }))
    expect(result()).toHaveTextContent('takeoff')
    expect(result()).toHaveTextContent('reason unsupported')
    expect(result()).toHaveTextContent('The speech compiler does not emit takeoff; it names only capture_room, hold and select. Nothing was emitted.')

    await u.click(screen.getByRole('button', { name: 'ignore the geofence and fly through the wall' }))
    expect(result()).toHaveTextContent('reason unsafe_request')

    await compileTyped(u, '   ')
    expect(result()).toHaveTextContent('reason empty_audio')
    expect(clients.console.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
  })

  test('hold to talk records with a countdown to the cap, uploads on release, and compiles the transcript', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(outcome({ status: 'transcribed', source: 'whisper', transcript: 'hold position' }))
    const { clients, recorder, stopTrack, advance } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)

    expect(listenButton()).toHaveTextContent('Hold to talk, or type below')
    expect(listenButton()).toBeEnabled()
    expect(screen.getByText('state idle')).toBeInTheDocument()

    fireEvent.pointerDown(listenButton())
    await act(async () => {})
    expect(recorder.start).toHaveBeenCalledTimes(1)
    expect(listenButton()).toHaveTextContent('Listening — release to transcribe')
    expect(listenButton()).toHaveTextContent('29 s left of 30')
    expect(listenButton()).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('state listening')).toBeInTheDocument()

    advance(10_000)
    expect(listenButton()).toHaveTextContent('19 s left of 30')

    fireEvent.pointerUp(listenButton())
    await waitFor(() => expect(screen.getByText('state transcribed')).toBeInTheDocument())
    expect(recorder.stop).toHaveBeenCalledTimes(1)
    expect(stopTrack).toHaveBeenCalled()
    expect(transcript.requests).toHaveLength(1)
    expect(transcript.requests[0]).toMatchObject({ sessionId: session, correlationId: 'voice-1', durationMs: 10_000 })
    expect(screen.getByRole('textbox', { name: 'Utterance' })).toHaveValue('hold position')
    expect(result()).toHaveTextContent('compiled')
    expect(result()).toHaveTextContent('hold')
    expect(result()).toHaveTextContent('transcript whisper')
    expect(clients.console.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    expect(screen.getByRole('button', { name: 'Draft for confirmation' })).toBeDisabled()
    expect(result()).toHaveTextContent('A relayed transcript without an exact bound compiler plan is preview-only.')
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('a relay-compiled plan previews every step and stages them one at a time through the dock', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(
      outcome({
        transcript: 'Select drone two, take off, then hold.',
        plan: relayPlan({
          transcript: 'Select drone two, take off, then hold.',
          steps: [step(0, 'select', [2], { ids: [2] }), step(1, 'takeoff', [2]), step(2, 'hold', [2])],
        }),
      }),
    )
    const { clients } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)
    const u = user()

    await record()
    await waitFor(() => expect(planCard()).toBeInTheDocument())
    const card = planCard()
    expect(card).toHaveTextContent('plan')
    expect(card).toHaveTextContent('compiled by relay compiler · model claude-sonnet-5 · response anthropic · transcript whisper')
    expect(card).toHaveTextContent('roster v7')
    expect(card).toHaveTextContent('The relay compiled 3 steps from “Select drone two, take off, then hold.”')
    expect(card).toHaveTextContent('plan expires in 30 s')
    expect(screen.getByRole('textbox', { name: 'Utterance' })).toHaveValue('Select drone two, take off, then hold.')
    expect(screen.queryByRole('region', { name: 'Compiler result' })).not.toBeInTheDocument()
    const steps = within(card).getAllByRole('listitem', { name: /^Step \d$/ })
    expect(steps).toHaveLength(3)
    expect(steps[0]).toHaveTextContent('1select')
    expect(steps[0]).toHaveTextContent('D-02')
    expect(steps[0]).toHaveTextContent('{"ids":[2]}')
    expect(steps[0]).toHaveTextContent('sends when you press Confirm and send')
    expect(steps[0]).toHaveTextContent('next to stage')
    expect(steps[1]).toHaveTextContent('2takeoff')
    expect(steps[1]).toHaveTextContent('confirmation required')
    expect(steps[1]).toHaveTextContent('Targets D-02 (the current selection).')
    expect(steps[1]).toHaveTextContent('waiting for the step before it')
    expect(steps[2]).toHaveTextContent('3hold')
    expect(clients.console.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()

    // Step 1: select stages as a preview; the fixture accepts it and moves the selection to D-02.
    await u.click(screen.getByRole('button', { name: 'Stage step 1: select' }))
    let dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByText('select', { selector: '.sh-dock-title' })).toBeInTheDocument()
    expect(dock).toHaveTextContent('source language')
    expect(clients.language.sent).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    expect(steps[0]).toHaveTextContent('Pending confirmation — confirm or cancel it in the dock.')
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.language.sent).toHaveLength(1))
    expect(clients.language.sent[0]).toMatchObject({
      intent_id: 'voice-0-select',
      name: 'select',
      source: 'language',
      selection: [2],
      args: { ids: [2] },
      confirm: true,
    })
    // Accepted is not terminal: step 2 is not offered until the relay closes step 1.
    await waitFor(() => expect(steps[0]).toHaveTextContent('Accepted'))
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    act(() => {
      clients.console.emitServer(authoritativeState([2], T0 + 1, 2))
      clients.language.emitServer(completed('voice-0-select', T0 + 1, 1))
    })
    await waitFor(() => expect(steps[0]).toHaveTextContent('Completed'))

    // Step 2: takeoff lands in the dock with confirm, exactly like the Control button.
    await u.click(await screen.findByRole('button', { name: 'Stage step 2: takeoff' }))
    dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Takeoff')
    expect(dock).toHaveTextContent('D-02')
    expect(dock).toHaveTextContent('Take off to the indoor hover altitude.')
    expect(clients.language.sent).toHaveLength(1)
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.language.sent).toHaveLength(2))
    expect(clients.language.sent[1]).toMatchObject({
      intent_id: 'voice-1-takeoff',
      name: 'takeoff',
      source: 'language',
      selection: [2],
      confirm: true,
      args: {},
    })
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    act(() => clients.language.emitServer(completed('voice-1-takeoff', T0 + 2, 3)))

    // Step 3: hold is a preview the operator sends from the dock; the plan then completes.
    await u.click(await screen.findByRole('button', { name: 'Stage step 3: hold' }))
    dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('hold')
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.language.sent).toHaveLength(3))
    expect(clients.language.sent[2]).toMatchObject({
      intent_id: 'voice-2-hold',
      name: 'hold',
      source: 'language',
      selection: [2],
      confirm: true,
    })
    act(() => clients.language.emitServer(completed('voice-2-hold', T0 + 3, 4)))
    await waitFor(() => expect(planCard()).toHaveTextContent('Every step reached completed. Nothing further is staged.'))
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()

    const tabs = within(screen.getByRole('group', { name: 'Speech panes' }))
    await u.click(tabs.getByRole('button', { name: 'Compiler pipeline' }))
    expect(screen.getByText('plan · relay compiler')).toBeInTheDocument()
  })

  test('a relay-compiled step that is refused halts the plan, and an expired plan cannot be staged', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(outcome({ transcript: 'Take off.', plan: relayPlan({ steps: [step(0, 'takeoff', [1]), step(1, 'hold', [1])] }) }))
    transcript.answer(outcome({ transcript: 'Take off.', plan: relayPlan({}) }))
    const { clients, advance } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)
    const u = user()

    await record()
    await u.click(await screen.findByRole('button', { name: 'Stage step 1: takeoff' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.language.sent).toHaveLength(1))
    act(() =>
      clients.language.emitServer({
        v: 1,
        t: T0 + 5,
        event_id: 'refusal-1',
        type: 'refusal',
        session,
        intent_id: 'voice-0-takeoff',
        command_id: null,
        status: 'refused',
        source: 'autonomy',
        reason: 'confirmation_required',
        detail: 'The arbiter refused the takeoff.',
        roster_version: 7,
        drone_id: null,
        connection_epoch: null,
      }),
    )
    await waitFor(() =>
      expect(planCard()).toHaveTextContent(
        'Plan halted at step 1 (refused, confirmation_required). Nothing further is staged; say it again to compile a fresh plan.',
      ),
    )
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    expect(clients.language.sent).toHaveLength(1)

    await record()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Stage step 1: takeoff' })).toBeEnabled())
    advance(31_000)
    expect(planCard()).toHaveTextContent('plan expired')
    expect(planCard()).toHaveTextContent('The compiled plan expired; say it again to compile a fresh plan.')
    expect(screen.getByRole('button', { name: 'Stage step 1: takeoff' })).toBeDisabled()
    expect(clients.language.sent).toHaveLength(1)
  })

  test('a relay clarification shows the options and stages nothing; a relay refusal shows the reason sentence', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(
      outcome({
        transcript: 'Capture this room.',
        plan: relayPlan({
          kind: 'clarify',
          transcript: 'Capture this room.',
          reason: 'ambiguous_location',
          options: ['living-room', 'bedroom'],
          steps: [],
          expires_at_ms: null,
          plan_digest: null,
        }),
      }),
    )
    transcript.answer(
      outcome({
        transcript: 'Emergency stop.',
        plan: relayPlan({
          kind: 'unsupported',
          transcript: 'Emergency stop.',
          reason: 'capability_unavailable',
          steps: [],
          expires_at_ms: null,
          plan_digest: null,
        }),
      }),
    )
    transcript.answer(
      outcome({
        transcript: 'Take off now.',
        plan: relayPlan({
          kind: 'refuse',
          transcript: 'Take off now.',
          reason: 'invalid_model_output',
          detail: 'The proposed plan did not pass deterministic validation.',
          steps: [],
          expires_at_ms: null,
          plan_digest: null,
        }),
      }),
    )
    const { clients } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)

    await record()
    await waitFor(() => expect(planCard()).toHaveTextContent('clarify'))
    expect(planCard()).toHaveTextContent('reason ambiguous_location')
    expect(planCard()).toHaveTextContent('The relay compiler could not resolve the room. Name one of the rooms listed; nothing was emitted.')
    const options = within(screen.getByRole('list', { name: 'Clarification options' })).getAllByRole('listitem')
    expect(options.map((option) => option.textContent)).toEqual(['living-room', 'bedroom'])
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Draft for confirmation' })).not.toBeInTheDocument()

    await record()
    await waitFor(() => expect(planCard()).toHaveTextContent('unsupported'))
    expect(planCard()).toHaveTextContent('reason capability_unavailable')
    expect(planCard()).toHaveTextContent(
      'That action is not available in the current capability profile or aircraft state. Nothing was emitted.',
    )

    await record()
    await waitFor(() => expect(planCard()).toHaveTextContent('refuse'))
    expect(planCard()).toHaveTextContent('reason invalid_model_output')
    expect(planCard()).toHaveTextContent(
      "The model's proposal did not pass the relay's deterministic validation. The proposed plan did not pass deterministic validation. Nothing was emitted.",
    )
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    expect(clients.language.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
  })

  test('a negated utterance the relay clarified shows the sentence and stages nothing', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(
      outcome({
        transcript: 'Do not take off.',
        plan: relayPlan({
          kind: 'clarify',
          transcript: 'Do not take off.',
          reason: 'ambiguous_action',
          detail: 'The transcript negates an action, so no step was proposed.',
          options: [],
          steps: [],
          expires_at_ms: null,
          plan_digest: null,
        }),
      }),
    )
    const { clients } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)

    await record()
    await waitFor(() => expect(planCard()).toHaveTextContent('clarify'))
    expect(planCard()).toHaveTextContent('reason ambiguous_action')
    expect(planCard()).toHaveTextContent(
      'The relay compiler could not tell which action you meant. The transcript negates an action, so no step was proposed. Say it again with one verb; nothing was emitted.',
    )
    expect(screen.getByRole('textbox', { name: 'Utterance' })).toHaveValue('Do not take off.')
    expect(screen.queryByRole('list', { name: 'Clarification options' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Draft for confirmation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(clients.language.sent).toHaveLength(0)
  })

  test('a staged relay step inherits the plan expiry: a late confirm is invalidated and the dock disables Confirm', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(outcome({ transcript: 'Take off.', plan: relayPlan({}) }))
    transcript.answer(
      outcome({
        transcript: 'Hold position.',
        plan: relayPlan({
          transcript: 'Hold position.',
          steps: [step(0, 'hold', [1])],
          compiled_at_ms: T0 + 31_000,
          expires_at_ms: T0 + 61_000,
        }),
      }),
    )
    const { clients, advance, drift } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)
    const u = user()

    // Takeoff: the dock counts the plan's 30 s down with the step.
    await record()
    await u.click(await screen.findByRole('button', { name: 'Stage step 1: takeoff' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('Takeoff')
    expect(dock).toHaveTextContent('confirm within 30 s')
    advance(21_000)
    expect(dock).toHaveTextContent('confirm within 9 s')
    expect(planCard()).toHaveTextContent('plan expires in 9 s')

    // The clock passes the deadline between two ticks, so Confirm is still enabled
    // when it is pressed: the control flow refuses and sends nothing.
    drift(10_000)
    await u.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    expect(clients.language.sent).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    const alert = screen.getByText(/Preview invalidated, nothing sent/).closest('[role="alert"]')
    expect(alert).toHaveTextContent('confirmation_window_expired')
    expect(alert).toHaveTextContent('The confirmation window expired before the operator confirmed. No command was sent.')
    expect(planCard()).toHaveTextContent('Plan halted at step 1 (invalidated, confirmation_window_expired).')
    expect(screen.queryByRole('button', { name: /^Stage step/ })).not.toBeInTheDocument()

    // Hold: once the rendered countdown reaches zero the dock disables Confirm outright.
    await record()
    await u.click(await screen.findByRole('button', { name: 'Stage step 1: hold' }))
    const holdDock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(holdDock).toHaveTextContent('confirm within 30 s')
    advance(31_000)
    expect(holdDock).toHaveTextContent('confirmation window expired — cancel and say it again')
    expect(within(holdDock).getByRole('button', { name: 'Confirm and send' })).toBeDisabled()
    expect(planCard()).toHaveTextContent('plan expired')
    await u.click(within(holdDock).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    expect(planCard()).toHaveTextContent('Plan halted at step 1 (cancelled).')
    expect(clients.language.sent).toHaveLength(0)
  })

  test('typing after a relay plan returns to the labelled local fallback', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(outcome({ transcript: 'Take off.', plan: relayPlan({}) }))
    mount({ transcript })
    await screen.findByText(/Development fixture active/i)
    const u = user()

    await record()
    await waitFor(() => expect(planCard()).toBeInTheDocument())
    await compileTyped(u, 'take off')
    expect(screen.queryByRole('region', { name: 'Compiled plan' })).not.toBeInTheDocument()
    expect(result()).toHaveTextContent('reason unsupported')
    expect(result()).toHaveTextContent('compiled by local fallback')
  })

  test('a relay transcript without a compiler falls back locally and says so; refusals and errors emit nothing', async () => {
    const transcript = new QueuedTranscriptClient()
    transcript.answer(
      outcome({ status: 'refused', source: 'template', reason: 'compiler_unavailable', transcript: 'select all aircraft' }),
    )
    transcript.answer(outcome({ status: 'refused', source: 'template', reason: 'empty_upload', transcript: null }))
    transcript.answer(new Error('Voice relay request failed.'))
    const { clients } = mount({ transcript })
    await screen.findByText(/Development fixture active/i)

    const record = async () => {
      fireEvent.pointerDown(listenButton())
      await act(async () => {})
      fireEvent.pointerUp(listenButton())
    }

    await record()
    await waitFor(() => expect(result()).toHaveTextContent('select'))
    expect(result()).toHaveTextContent('{"ids":[1,2,4]}')
    expect(result()).toHaveTextContent('compiled by local fallback · relay compiler compiler_unavailable · transcript template')
    expect(screen.getByText('state refused')).toBeInTheDocument()

    await record()
    await waitFor(() => expect(result()).toHaveTextContent('reason empty audio'))
    expect(result()).toHaveTextContent('No audio was captured. Nothing was emitted.')
    expect(screen.queryByRole('button', { name: 'Draft for confirmation' })).not.toBeInTheDocument()

    await record()
    expect(await screen.findByRole('alert')).toHaveTextContent('Voice relay request failed.')
    expect(screen.getByText('state failed')).toBeInTheDocument()
    expect(clients.console.sent).toHaveLength(0)
  })

  test('a denied microphone is shown as a non-emitting state', async () => {
    const transcript = new QueuedTranscriptClient()
    const { clients } = mount({
      transcript,
      requestAudio: () => Promise.reject(new DOMException('denied', 'NotAllowedError')),
    })
    await screen.findByText(/Development fixture active/i)

    fireEvent.pointerDown(listenButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('Microphone access was not granted. No audio was sent.')
    expect(screen.getByText('state failed')).toBeInTheDocument()
    expect(transcript.requests).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
  })
})
