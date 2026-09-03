import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import App from './App'
import { FixtureRelayClient } from './testing/fixture-relay-client'
import type { TranscriptClient, VoiceOutcome } from './voice/client'
import type { RecorderFactory } from './voice/use-push-to-talk'

const session = 'voice-test-session'
const clock = () => 1_756_700_000_000

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
    this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) } as BlobEvent)
    this.onstop?.()
  })
}

// Voice is disabled behind the fixture banner, so present the fixture as a live transport.
class LiveLookingFixtureRelayClient extends FixtureRelayClient {
  override readonly transport = 'websocket' as unknown as 'fixture'
}

function renderConsole(outcome: Partial<VoiceOutcome>) {
  const consoleClient = new LiveLookingFixtureRelayClient(session, clock, 'console')
  const keyboardClient = new LiveLookingFixtureRelayClient(session, clock, 'keyboard')
  const recorder = new FakeRecorder()
  const transcriptClient: TranscriptClient = {
    transcribe: vi.fn().mockResolvedValue({
      v: 1,
      type: 'voice_outcome',
      session,
      correlation_id: 'voice-1',
      status: 'transcribed',
      source: 'whisper',
      reason: null,
      transcript: null,
      emissions: [],
      ...outcome,
    }),
  }
  render(
    <App
      sessionId={session}
      clients={{ console: consoleClient, keyboard: keyboardClient }}
      transcriptClient={transcriptClient}
      voiceOptions={{
        requestAudio: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream,
        recorderFactory: (() => recorder) as RecorderFactory,
        nextId: () => 'voice-1',
      }}
    />,
  )
  return { recorder, transcriptClient }
}

describe('voice outcome rendering', () => {
  test('shows the transcript and the absence of a plan after a transcribed result', async () => {
    const { recorder } = renderConsole({ transcript: 'hold drone one', status: 'transcribed' })
    await screen.findByRole('button', { name: 'Hold to record a voice plan' })

    fireEvent.keyDown(window, { key: ' ' })
    await waitFor(() => expect(recorder.start).toHaveBeenCalledTimes(1))
    fireEvent.keyUp(window, { key: ' ' })

    const card = await screen.findByTestId('voice-outcome')
    expect(card).toHaveTextContent('Transcribed')
    expect(card).toHaveTextContent('hold drone one')
    expect(card).toHaveTextContent('No plan was emitted')
  })

  test('shows the refusal reason distinctly, with the transcript when the relay heard something', async () => {
    const { recorder } = renderConsole({
      status: 'refused',
      source: 'template',
      reason: 'compiler_unavailable',
      transcript: 'capture the room',
    })
    await screen.findByRole('button', { name: 'Hold to record a voice plan' })

    fireEvent.keyDown(window, { key: ' ' })
    await waitFor(() => expect(recorder.start).toHaveBeenCalledTimes(1))
    fireEvent.keyUp(window, { key: ' ' })

    const card = await screen.findByTestId('voice-outcome')
    expect(card).toHaveTextContent('Refused')
    expect(card).toHaveTextContent('capture the room')
    expect(card).toHaveTextContent('transcript compiler is not available')
    expect(card).toHaveTextContent('compiler_unavailable')
  })

  test('Space inside the room identifier field types a space and does not record', async () => {
    const { recorder } = renderConsole({ transcript: 'unused' })
    const user = userEvent.setup()
    const field = await screen.findByLabelText('Room identifier')

    await user.click(field)
    await user.keyboard(' ')

    expect(field).toHaveValue('room-01 ')
    expect(recorder.start).not.toHaveBeenCalled()
  })
})
