import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { MemoryPanel } from './MemoryPanel'
import MemoryDialog from './MemoryDialog'
import MemoryLibrary from './MemoryLibrary'
import { EMPTY_NOTES, type MemoryContext } from './types'

const initialShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
beforeEach(() => {
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = vi.fn(() => 'blob:unit-test')
      static revokeObjectURL = vi.fn()
    },
  )
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (initialShowModal)
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', initialShowModal)
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
})
const capture: Capture = {
  id: 'capture',
  name: 'Sam',
  contributor_id: 'sam',
  kind: 'photo',
  source: 'import',
  captured_at: null,
  position: null,
  uploaded_at: 1234,
  note: '',
  mime: 'image/png',
  bytes: 100,
  sha256: 'checksum',
}
const inspection = {
  mime: 'image/png',
  has_audio: false,
  timestamp: null,
  local_timestamp: null,
  location: null,
  warnings: [],
}
const context = (changes: Partial<MemoryContext> = {}): MemoryContext => ({
  revision: 0,
  capture,
  notes: { ...EMPTY_NOTES },
  assets: [],
  inspection: null,
  analysis: null,
  can_edit: true,
  capabilities: { metadata: true, ai: true, weather: true, media_tools: true },
  ...changes,
})
function setup(value = context()) {
  const client = new AtlasClient({
    baseUrl: 'https://example.test',
    sessionId: 'room',
    token: 'unit-test-only',
  })
  vi.spyOn(client, 'memory').mockResolvedValue(value)
  vi.spyOn(client, 'inspectMemory').mockResolvedValue({ ...value, inspection })
  vi.spyOn(client, 'media').mockResolvedValue(new Blob(['photo'], { type: 'image/png' }))
  return client
}
const click = async (name: string | RegExp) =>
  userEvent.click(await screen.findByRole('button', { name }))
const reviewed = (value: MemoryContext) => ({
  ...value,
  review: { revision: value.revision, analysis_id: value.analysis?.id ?? null, reviewed_at: 1234 },
})
const song = {
  id: 'song',
  title: 'Our song',
  role: 'soundtrack' as const,
  mime: 'audio/mpeg',
  bytes: 30,
  sha256: 'a',
  added_at: 0,
  rights_confirmed: true as const,
}

it('starts compact, reads local metadata automatically, and keeps external assistance opt-in', async () => {
  const client = setup()
  const analyze = vi.spyOn(client, 'analyzeMemory').mockResolvedValue(context())
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await screen.findByLabelText('What made this moment memorable?')
  expect(screen.queryByLabelText('When did this happen?')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Choose audio or video')).not.toBeInTheDocument()
  await waitFor(() => expect(client.inspectMemory).toHaveBeenCalledTimes(1))
  expect(analyze).not.toHaveBeenCalled()
  await click(/Let the details come to you/)
  expect(screen.getByLabelText(/Draft my memory with AI/)).not.toBeChecked()
  expect(screen.getByLabelText(/Bring back the weather/)).not.toBeChecked()
  expect(screen.getByLabelText(/Bring back the weather/)).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Find the details' })).toBeDisabled()
  await click(/Confirm place & time/)
  await userEvent.click(screen.getByText('Enter or adjust details manually'))
  expect(screen.getByLabelText('When did this happen?')).toHaveValue('')
  expect(screen.getByLabelText('Latitude')).toHaveValue('')
})

it('keeps a memory in one action, saving contributor notes before reviewing the new revision', async () => {
  const client = setup()
  let saved = context()
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockImplementation(async (_s, _c, _r, notes) => (saved = context({ revision: 1, notes })))
  const review = vi.spyOn(client, 'reviewMemory').mockImplementation(async () => reviewed(saved))
  const done = vi.fn()
  render(<MemoryPanel client={client} spaceId="place" capture={capture} onDone={done} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), {
    target: { value: 'An evening at Shoal Creek.' },
  })
  await click('Joyful')
  await click('Keep this memory')
  await screen.findByText(/Memory kept/)
  expect(save).toHaveBeenCalledWith('place', 'capture', 0, {
    ...EMPTY_NOTES,
    description: 'An evening at Shoal Creek.',
    feeling: 'Joyful',
  })
  expect(review).toHaveBeenCalledWith('place', 'capture', 1, null)
  await click('Done')
  expect(done).toHaveBeenCalledOnce()
})

it('preserves generated provenance when approving a completed draft', async () => {
  const analysis = {
    id: 'job',
    status: 'complete' as const,
    started_at: 0,
    suggestion: {
      model: 'test',
      summary: 'AI sample, not your own words.',
      visual_observations: [],
      atmosphere_suggestions: [],
      uncertainties: [],
    },
  }
  const value = context({ analysis })
  const client = setup(value)
  const save = vi.spyOn(client, 'saveMemory')
  const review = vi.spyOn(client, 'reviewMemory').mockResolvedValue(reviewed(value))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await screen.findByText(analysis.suggestion.summary)
  await click('Keep this memory')
  await screen.findByText(/Memory kept/)
  expect(save).not.toHaveBeenCalled()
  expect(review).toHaveBeenCalledWith('place', 'capture', 0, 'job')
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('')
  expect(screen.getByText('AI DRAFT · YOUR REVIEW')).toBeInTheDocument()
})

it('saves dirty notes before assistance and never calls providers when saving fails', async () => {
  const client = setup()
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockRejectedValueOnce(new Error('Connection lost. Retry when online.'))
    .mockImplementation(async (_s, _c, _r, notes) => context({ notes, revision: 1 }))
  const analyze = vi.spyOn(client, 'analyzeMemory').mockResolvedValue(context())
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), {
    target: { value: 'My own recollection' },
  })
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost')
  expect(analyze).not.toHaveBeenCalled()
  await click('Find the details')
  await waitFor(() =>
    expect(analyze).toHaveBeenCalledWith('place', 'capture', {
      revision: 1,
      weather: false,
      ai: true,
      audio_asset_id: null,
    }),
  )
  expect(save).toHaveBeenCalledTimes(2)
})

it('does not let a late metadata response replace saved notes or their revision', async () => {
  const client = setup()
  let inspected!: (value: MemoryContext) => void
  vi.mocked(client.inspectMemory).mockReturnValue(
    new Promise((resolve) => {
      inspected = resolve
    }),
  )
  let saved = context()
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockImplementation(
      async (_s, _c, revision, notes) => (saved = context({ notes, revision: revision + 1 })),
    )
  vi.spyOn(client, 'reviewMemory').mockImplementation(async () => reviewed(saved))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), {
    target: { value: 'First thought' },
  })
  await click('Keep this memory')
  await screen.findByText(/Memory kept/)
  await act(async () => inspected(context({ inspection })))
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('First thought')
  fireEvent.change(screen.getByLabelText('What made this moment memorable?'), {
    target: { value: 'Second thought' },
  })
  await click('Keep this memory')
  await waitFor(() =>
    expect(save).toHaveBeenLastCalledWith(
      'place',
      'capture',
      1,
      expect.objectContaining({ description: 'Second thought' }),
    ),
  )
})

it('keeps embedded time and place suggestions unconfirmed until selected', async () => {
  const client = setup(
    context({
      inspection: {
        ...inspection,
        timestamp: '2024-05-18T23:30:00Z',
        location: { latitude: 30.276, longitude: -97.75 },
      },
    }),
  )
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockImplementation(async (_s, _c, _r, notes) => context({ notes, revision: 1 }))
  vi.spyOn(client, 'reviewMemory').mockResolvedValue(reviewed(context({ revision: 1 })))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click('Place & time')
  expect(screen.getByText(/Time not set/)).toHaveTextContent('Place not set')
  await click('Use embedded time')
  await click('Use embedded location')
  await click('Keep this memory')
  expect(save).toHaveBeenCalledWith(
    'place',
    'capture',
    0,
    expect.objectContaining({
      occurred_at: '2024-05-18T23:30:00Z',
      location: { latitude: 30.276, longitude: -97.75 },
    }),
  )
})

it('keeps invitation playback usable, with no autoplay or owner-only writes', async () => {
  const client = setup(context({ can_edit: false, assets: [song] }))
  const media = vi.spyOn(client, 'memoryAssetMedia').mockResolvedValue(new Blob(['sound']))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  expect(await screen.findByText(/Your invitation can view/)).toBeInTheDocument()
  expect(client.inspectMemory).not.toHaveBeenCalled()
  expect(screen.getByLabelText('What made this moment memorable?')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Keep this memory' })).not.toBeInTheDocument()
  await click('1 recording')
  expect(media).not.toHaveBeenCalled()
  await click(/Load recording/)
  await waitFor(() =>
    expect(document.querySelector('audio')).toHaveAttribute('src', 'blob:unit-test'),
  )
  expect(document.querySelector('audio')).not.toHaveAttribute('autoplay')
  expect(screen.queryByLabelText('Choose audio or video')).not.toBeInTheDocument()
})

it('excludes music from transcription, resets consent on reentry, and restores keyboard focus', async () => {
  const client = setup(context({ assets: [song] }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  expect(screen.getByRole('heading', { name: 'Let the details come to you' })).toHaveFocus()
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  expect(
    within(screen.getByRole('group', { name: 'Audio for analysis' })).queryByText('Our song'),
  ).not.toBeInTheDocument()
  await click('Back to memory')
  expect(screen.getByRole('button', { name: /Let the details come to you/ })).toHaveFocus()
  await click(/Let the details come to you/)
  expect(screen.getByLabelText(/Draft my memory with AI/)).not.toBeChecked()
})

it('keeps the original preview loaded while moving between editors', async () => {
  const client = setup()
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await screen.findByAltText('Original capture by Sam')
  await click('Music')
  await userEvent.click(screen.getAllByRole('button', { name: 'Back to memory' })[0])
  expect(client.media).toHaveBeenCalledOnce()
  expect(screen.getByAltText('Original capture by Sam')).toBeVisible()
})

it('selects the only context recording for a photo without audio, without authorizing AI', async () => {
  const client = setup(
    context({ assets: [{ ...song, id: 'voice', title: 'My voice memory', role: 'narration' }] }),
  )
  const analyze = vi.spyOn(client, 'analyzeMemory').mockResolvedValue(context())
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  expect(analyze).not.toHaveBeenCalled()
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  expect(screen.getByRole('button', { name: 'My voice memory' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await click('Find the details')
  expect(analyze).toHaveBeenCalledWith('place', 'capture', {
    revision: 0,
    ai: true,
    weather: false,
    audio_asset_id: 'voice',
  })
})

it('protects unsaved notes and pending files on close and Escape', async () => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '')
    },
  })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false),
    close = vi.fn()
  const view = render(
    <MemoryDialog client={setup()} spaceId="place" capture={capture} onClose={close} />,
  )
  await click('Add sound')
  await userEvent.upload(
    screen.getByLabelText('Choose audio or video'),
    new File(['wav'], 'Birds.wav', { type: 'audio/wav' }),
  )
  expect(screen.getByRole('button', { name: 'Keep this memory' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Add recording' })).toBeDisabled()
  await click('Close dialog')
  expect(confirm).toHaveBeenCalledOnce()
  expect(close).not.toHaveBeenCalled()
  const event = new Event('cancel', { cancelable: true, bubbles: true })
  fireEvent(view.container.querySelector('dialog')!, event)
  expect(event.defaultPrevented).toBe(true)
  expect(close).not.toHaveBeenCalled()
})

it('uploads a named recording after saving notes, without requiring an extra form save', async () => {
  const client = setup()
  let saved = context()
  vi.spyOn(client, 'saveMemory').mockImplementation(
    async (_s, _c, _r, notes) => (saved = context({ notes, revision: 1 })),
  )
  const upload = vi.spyOn(client, 'uploadMemoryAsset').mockImplementation(async () => {
    vi.mocked(client.memory).mockResolvedValue({
      ...saved,
      revision: 2,
      assets: [{ ...song, title: 'Birds.wav', role: 'ambient' }],
    })
    return song
  })
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), {
    target: { value: 'Birds at Shoal Creek' },
  })
  await click('Add sound')
  const file = new File(['wav'], 'Birds.wav', { type: 'audio/wav' })
  await userEvent.upload(screen.getByLabelText('Choose audio or video'), file)
  await userEvent.click(screen.getByLabelText(/I recorded this/))
  await click('Add recording')
  await screen.findByText('Recording added.')
  expect(upload).toHaveBeenCalledWith(
    'place',
    'capture',
    file,
    { title: 'Birds.wav', role: 'ambient', rights_confirmed: true },
    expect.any(AbortSignal),
  )
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue(
    'Birds at Shoal Creek',
  )
})

it('shows unconfigured providers and native attachment limitations honestly', async () => {
  const client = setup(
    context({ capabilities: { metadata: true, ai: false, weather: false, media_tools: false } }),
  )
  vi.spyOn(client, 'memoryUploadsSupported', 'get').mockReturnValue(false)
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  expect(screen.getByLabelText(/Draft my memory with AI/)).toBeDisabled()
  expect(screen.getByLabelText(/Bring back the weather/)).toBeDisabled()
  await click('Back to memory')
  await click('Add sound')
  expect(screen.getByText(/Attach recordings from the web console/)).toBeInTheDocument()
  expect(screen.queryByLabelText('Choose audio or video')).not.toBeInTheDocument()
})

it('makes disconnected World Builder memories an honest empty state', () => {
  render(<MemoryLibrary />)
  expect(screen.getByText(/Connect an Atlas workspace/)).toBeInTheDocument()
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
})
