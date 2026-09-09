import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { MemoryPanel } from './MemoryPanel'
import MemoryDialog from './MemoryDialog'
import MemoryLibrary from './MemoryLibrary'
import { EMPTY_NOTES, type MemoryContext } from './types'

const initialShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
afterEach(() => {
  vi.restoreAllMocks()
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
  return client
}

it('keeps provider calls opt-in and never substitutes upload time or current location', async () => {
  const client = setup()
  const analyze = vi.spyOn(client, 'analyzeMemory').mockResolvedValue(context())
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  const user = userEvent.setup()
  expect(await screen.findByLabelText('When did this happen?')).toHaveValue('')
  expect(screen.getByLabelText('Latitude')).toHaveValue('')
  expect(screen.getByLabelText(/Look up weather/)).not.toBeChecked()
  expect(screen.getByLabelText(/Ask AI/)).not.toBeChecked()
  expect(analyze).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Inspect local context' }))
  expect(analyze).toHaveBeenCalledWith('place', 'capture', {
    revision: 0,
    weather: false,
    ai: false,
    audio_asset_id: null,
  })
})

it('saves contributor feelings and confirmed context with a revision before analysis', async () => {
  const client = setup()
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockImplementation(async (_s, _c, _r, notes) => context({ revision: 1, notes }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  const description = await screen.findByLabelText('What was happening?')
  fireEvent.change(description, { target: { value: 'An evening at Shoal Creek.' } })
  fireEvent.change(screen.getByLabelText('How did it feel to you?'), {
    target: { value: 'Joyful.' },
  })
  fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '30.276' } })
  fireEvent.change(screen.getByLabelText('Longitude'), { target: { value: '-97.75' } })
  expect(screen.getByRole('button', { name: 'Inspect local context' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Save memory details' }))
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith('place', 'capture', 0, {
      ...EMPTY_NOTES,
      description: 'An evening at Shoal Creek.',
      feeling: 'Joyful.',
      location: { latitude: 30.276, longitude: -97.75 },
    }),
  )
  expect(await screen.findByRole('button', { name: 'Memory details saved' })).toBeDisabled()
})

it('excludes soundtracks from analysis and does not autoplay recordings', async () => {
  const client = setup(
    context({
      assets: [
        {
          id: 'song',
          title: 'Our song',
          role: 'soundtrack',
          mime: 'audio/mpeg',
          bytes: 30,
          sha256: 'a',
          added_at: 0,
          rights_confirmed: true,
        },
      ],
    }),
  )
  const media = vi.spyOn(client, 'memoryAssetMedia')
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await screen.findByText('Our song')
  expect(
    within(screen.getByRole('group', { name: 'Audio for analysis' })).queryByText('Our song'),
  ).not.toBeInTheDocument()
  expect(media).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Attach recording' })).toBeDisabled()
})

it('protects unsaved memory details on close and Escape', async () => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '')
    },
  })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const close = vi.fn()
  const view = render(
    <MemoryDialog client={setup()} spaceId="place" capture={capture} onClose={close} />,
  )
  fireEvent.change(await screen.findByLabelText('What was happening?'), {
    target: { value: 'Unsaved memory' },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
  expect(confirm).toHaveBeenCalledOnce()
  expect(close).not.toHaveBeenCalled()
  const event = new Event('cancel', { cancelable: true, bubbles: true })
  fireEvent(view.container.querySelector('dialog')!, event)
  expect(event.defaultPrevented).toBe(true)
  expect(close).not.toHaveBeenCalled()
})

it('shows invitation read-only state and unavailable providers honestly', async () => {
  render(
    <MemoryPanel
      client={setup(
        context({
          can_edit: false,
          capabilities: { metadata: true, ai: false, weather: false, media_tools: false },
        }),
      )}
      spaceId="place"
      capture={capture}
    />,
  )
  expect(await screen.findByText(/Your invitation can view/)).toBeInTheDocument()
  expect(screen.getByLabelText('What was happening?')).toBeDisabled()
  expect(screen.getByLabelText(/Ask AI/)).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Attach recording' })).not.toBeInTheDocument()
})

it('makes disconnected World Builder memories an honest empty state', () => {
  render(<MemoryLibrary />)
  expect(screen.getByText(/Connect an Atlas workspace/)).toBeInTheDocument()
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
})
