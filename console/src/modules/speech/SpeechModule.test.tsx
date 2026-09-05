import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from '../../App'
import { FixtureRelayClient } from '../../testing/fixture-relay-client'
import type { TranscriptClient, TranscriptRequest, VoiceOutcome } from '../../voice/client'
import type { RecorderFactory } from '../../voice/use-push-to-talk'
import type { VoiceDependencies } from '../types'

const session = 'speech-module-session'

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
    return { ...next, session: request.sessionId, correlation_id: request.correlationId }
  }
}

function outcome(overrides: Partial<VoiceOutcome>): VoiceOutcome {
  return {
    v: 1,
    type: 'voice_outcome',
    status: 'transcribed',
    source: 'whisper',
    reason: null,
    transcript: null,
    emissions: [],
    ...overrides,
  }
}

function mount(options: { transcript?: TranscriptClient; requestAudio?: () => Promise<MediaStream> } = {}) {
  let current = 1_756_700_000_000
  const now = () => current
  let sequence = 0
  const clients = {
    console: new FixtureRelayClient(session, now, 'console'),
    keyboard: new FixtureRelayClient(session, now, 'keyboard'),
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
  }
}

const listenButton = () => screen.getByRole('button', { name: /Hold to talk|Listening|Language disabled|Transcribing/ })
const result = () => screen.getByRole('region', { name: 'Compiler result' })
const user = () => userEvent.setup()

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

    await user().click(screen.getByRole('button', { name: 'Draft for confirmation' }))
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).toHaveTextContent('hold')
    expect(clients.console.sent).toHaveLength(0)

    await user().click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    await waitFor(() => expect(clients.console.sent).toHaveLength(1))
    expect(clients.console.sent[0]).toMatchObject({
      intent_id: 'speech-intent-1',
      name: 'hold',
      source: 'console',
      selection: [1],
      confirm: true,
      args: {},
    })
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
