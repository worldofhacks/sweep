import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { MemoryPanel } from './MemoryPanel'
import MemoryDialog from './MemoryDialog'
import MemoryLibrary from './MemoryLibrary'
import { AssistEditor } from './MemoryEditors'
import { EMPTY_NOTES, type MemoryContext } from './types'

const initialShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  })
  vi.stubGlobal('navigator', { ...navigator, locks: { request: async (_key: string, action: () => unknown) => action() } })
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
function setup(value = context(), token = 'unit-test-only') {
  const client = new AtlasClient({
    baseUrl: 'https://example.test',
    sessionId: 'room',
    token,
  })
  vi.spyOn(client, 'memory').mockResolvedValue(value)
  vi.spyOn(client, 'inspectMemory').mockResolvedValue({ ...value, inspection })
  vi.spyOn(client, 'media').mockResolvedValue(new Blob(['photo'], { type: 'image/png' }))
  return client
}
const click = async (name: string | RegExp) =>
  userEvent.click(await screen.findByRole('button', { name }))

const dailyAllowance = (remaining = 0): NonNullable<MemoryContext['analysis_allowance']> => ({
  unit: 'provider_stage_reservation', limits: { space: 3, relay: 10 },
  reserved: { space: 3 - remaining, relay: 3 - remaining },
  remaining, resets_at: Date.UTC(2026, 8, 10), error: null,
})

it('explains exhausted allowance before submission while keeping local analysis available', async () => {
  const onAnalyze = vi.fn()
  render(<AssistEditor data={context({ analysis_allowance: dailyAllowance() })}
    notes={EMPTY_NOTES} coordinates={['', '']} disabled={false} onPlace={vi.fn()} onAnalyze={onAnalyze} />)
  expect(screen.getByRole('status', { name: 'External assistance allowance' })).toHaveTextContent('External assistance is paused')
  expect(screen.getByRole('checkbox', { name: /Draft my memory/ })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: /Bring back/ })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Find the details' })).toBeDisabled()
  await userEvent.click(screen.getByText('Local analysis & setup details'))
  await userEvent.click(screen.getByRole('button', { name: 'Inspect local context' }))
  expect(onAnalyze).toHaveBeenCalledExactlyOnceWith({ weather: false, ai: false, audio_asset_id: null })
})

it('keeps allowance changes explicit and never silently submits selected providers', async () => {
  const onAnalyze = vi.fn()
  const props = { notes: EMPTY_NOTES, coordinates: ['', ''], disabled: false, onPlace: vi.fn(), onAnalyze }
  const { rerender } = render(<AssistEditor {...props} data={context({ analysis_allowance: dailyAllowance(2) })} />)
  expect(screen.getByRole('status')).toHaveTextContent('2 provider requests available today')
  expect(screen.getByRole('status')).toHaveTextContent('not a price or billing balance')
  await userEvent.click(screen.getByRole('checkbox', { name: /Draft my memory/ }))
  expect(screen.getByRole('button', { name: 'Find the details' })).toBeEnabled()
  rerender(<AssistEditor {...props} data={context({ analysis_allowance: dailyAllowance() })} />)
  expect(screen.getByRole('button', { name: 'Find the details' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: /Draft my memory/ })).not.toBeChecked()
  expect(onAnalyze).not.toHaveBeenCalled()
})

it('distinguishes missing deployment setup from an exhausted daily allowance', () => {
  const value = dailyAllowance()
  value.limits.space = 0
  render(<AssistEditor data={context({ analysis_allowance: value })}
    notes={EMPTY_NOTES} coordinates={['', '']} disabled={false} onPlace={vi.fn()} onAnalyze={vi.fn()} />)
  expect(screen.getByRole('status')).toHaveTextContent('Ask the workspace operator to set daily request allowances')
  expect(screen.getByRole('status')).not.toHaveTextContent('Resets')
})

it('keeps interrupted work locked and stops only the explicit current analysis', async () => {
  const value = context({
    can_cancel: true,
    analysis: { id: 'analysis-id', status: 'interrupted', started_at: 1234 },
  })
  const client = setup(value)
  const cancel = vi.spyOn(client, 'cancelMemory').mockResolvedValue({
    ...value, analysis: { ...value.analysis!, status: 'cancelling' },
  })
  const analyze = vi.spyOn(client, 'analyzeMemory')
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  expect(await screen.findByLabelText('What made this moment memorable?')).toBeDisabled()
  expect(screen.getByText(/worker has not confirmed completion/)).toBeVisible()
  expect(client.inspectMemory).not.toHaveBeenCalled()
  expect(cancel).not.toHaveBeenCalled()
  await click('Stop analysis')
  expect(cancel).toHaveBeenCalledExactlyOnceWith('place', 'capture', 'analysis-id')
  expect(await screen.findByText(/Stop requested. Waiting/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Stop analysis' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('What made this moment memorable?')).toBeDisabled()
  expect(analyze).not.toHaveBeenCalled()
})

it('polls cancelling work until confirmed stopped, without retrying the analysis', async () => {
  vi.useFakeTimers()
  try {
    const value = context({
      can_cancel: true,
      analysis: { id: 'analysis-id', status: 'cancelling', started_at: 1234 },
    })
    const client = setup(value)
    const analyze = vi.spyOn(client, 'analyzeMemory')
    const cancel = vi.spyOn(client, 'cancelMemory')
    render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
    await act(async () => { await Promise.resolve() })
    vi.mocked(client.memory).mockResolvedValue({
      ...value, can_cancel: false, analysis: { ...value.analysis!, status: 'cancelled' },
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(screen.getByText(/Analysis stopped. No new generated context/)).toBeVisible()
    expect(screen.getByLabelText('What made this moment memorable?')).not.toBeDisabled()
    const reads = vi.mocked(client.memory).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(client.memory).toHaveBeenCalledTimes(reads)
    expect(analyze).not.toHaveBeenCalled()
    expect(cancel).not.toHaveBeenCalled()
  } finally {
    vi.useRealTimers()
  }
})

it('keeps removal behind details, explicit server capability, and a clean editor', async () => {
  const client = setup(context({ can_remove: true }))
  const onRemove = vi.fn()
  render(<MemoryPanel client={client} spaceId="space" capture={capture} onRemove={onRemove} />)
  await screen.findByLabelText('What made this moment memorable?')
  expect(screen.queryByRole('button', { name: 'Review removal from this Space' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('What made this moment memorable?'), { target: { value: 'Unsaved story' } })
  await click('Details & sources')
  expect(screen.getByRole('button', { name: 'Review removal from this Space' })).toBeDisabled()
  expect(onRemove).not.toHaveBeenCalled()
})

it('does not advertise removal on older servers or unsupported native clients', async () => {
  const client = setup(context({ can_remove: true }))
  vi.spyOn(client, 'memoryRemovalSupported', 'get').mockReturnValue(false)
  render(<MemoryPanel client={client} spaceId="space" capture={capture} onRemove={vi.fn()} />)
  await click('Details & sources')
  expect(screen.queryByRole('button', { name: 'Review removal from this Space' })).not.toBeInTheDocument()
})
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

it('lets account contributors keep stories and sounds without offering operator analysis', async () => {
  const value = context({ can_analyze: false })
  const client = setup(value)
  const analyze = vi.spyOn(client, 'analyzeMemory')
  const save = vi
    .spyOn(client, 'saveMemory')
    .mockImplementation(async (_s, _c, _r, notes) => ({ ...value, notes, revision: 1 }))
  const review = vi
    .spyOn(client, 'reviewMemory')
    .mockResolvedValue(reviewed({ ...value, revision: 1 }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  const story = await screen.findByLabelText('What made this moment memorable?')
  expect(story).not.toBeDisabled()
  expect(screen.getByText(/shared with everyone who has access/)).toBeVisible()
  expect(
    screen.queryByRole('button', { name: /Let the details come to you/ }),
  ).not.toBeInTheDocument()
  await userEvent.type(story, 'A garden we grew together.')
  await click('Add sound')
  expect(screen.getByLabelText('Choose audio or video')).not.toBeDisabled()
  await userEvent.click(screen.getByLabelText('Back to memory'))
  await click('Keep this memory')
  await screen.findByText(/Memory kept/)
  expect(save).toHaveBeenCalledOnce()
  expect(review).toHaveBeenCalledWith('place', 'capture', 1, null)
  expect(analyze).not.toHaveBeenCalled()
})

it('preserves a contributor draft after a revision conflict and never reviews stale text', async () => {
  const client = setup(context({ can_analyze: false }))
  vi.spyOn(client, 'saveMemory').mockRejectedValue(
    new Error('This memory changed in another window. Reload before saving.'),
  )
  const review = vi.spyOn(client, 'reviewMemory')
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await userEvent.type(
    await screen.findByLabelText('What made this moment memorable?'),
    'Keep my unsaved words',
  )
  await click('Keep this memory')
  await screen.findByRole('alert')
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue(
    'Keep my unsaved words',
  )
  expect(review).not.toHaveBeenCalled()
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  await click('Reload saved context')
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue(
    'Keep my unsaved words',
  )
})

it('loads attributed history only on request and pages without exporting private content', async () => {
  const client = setup(context({ can_edit: false, can_analyze: false }))
  const entries = Array.from({ length: 20 }, (_, i) => ({
    sequence: 30 - i,
    revision: 30 - i,
    kind: 'notes' as const,
    actor: 'acct_1234567890',
    changed_at: 1700000000000,
    notes: { ...EMPTY_NOTES, description: `Saved story ${30 - i}` },
    previous: EMPTY_NOTES,
  }))
  const history = vi
    .spyOn(client, 'memoryHistory')
    .mockResolvedValueOnce(entries)
    .mockResolvedValueOnce([
      { ...entries[0], sequence: 10, notes: { ...EMPTY_NOTES, description: 'Earlier story' } },
    ])
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click('Details & sources')
  expect(history).not.toHaveBeenCalled()
  await click('Show edit history')
  await screen.findByText('Saved story 30')
  expect(screen.getByText(/History begins when this feature was enabled/)).toBeVisible()
  expect(screen.queryByText(/Reviewed by the owner/)).not.toBeInTheDocument()
  await click('Earlier edits')
  await screen.findByText('Earlier story')
  expect(history).toHaveBeenLastCalledWith('place', 'capture', expect.any(AbortSignal), 11)
  expect(screen.queryByText('Saved story 30')).not.toBeInTheDocument()
  await click('Hide edit history')
  expect(screen.queryByText('Earlier story')).not.toBeInTheDocument()
})

it('does not retain history from a refused next-page request', async () => {
  const client = setup(context({ can_edit: false }))
  vi.spyOn(client, 'memoryHistory')
    .mockResolvedValueOnce(
      Array.from({ length: 20 }, (_, i) => ({
        sequence: 30 - i,
        revision: 1,
        kind: 'review' as const,
        actor: 'workspace-operator',
        changed_at: 1700000000000,
      })),
    )
    .mockRejectedValueOnce(new Error('Access removed'))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click('Details & sources')
  await click('Show edit history')
  await click('Earlier edits')
  expect(await screen.findByRole('alert')).toHaveTextContent('History is unavailable')
  expect(screen.queryByText(/Edit 30/)).not.toBeInTheDocument()
})

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

it('recovers a lost analysis reply with the same identity and frozen settings, without saving twice', async () => {
  const value = context({ analysis_idempotency: true })
  const client = setup(value)
  const save = vi.spyOn(client, 'saveMemory').mockImplementation(async (_s, _c, _r, notes) => ({ ...value, notes, revision: 1 }))
  const analyze = vi.spyOn(client, 'analyzeMemory')
    .mockRejectedValueOnce(new Error('Reply lost'))
    .mockImplementation(async (_s, _c, request) => ({
      ...value, revision: 1, notes: { ...EMPTY_NOTES, description: 'My own words' },
      analysis: { id: 'job', request_id: request.request_id, status: 'complete', started_at: 1 },
      analysis_request: { id: request.request_id!, analysis_id: 'job', rejection: null, replayed: true, current: true },
    }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), { target: { value: 'My own words' } })
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  await screen.findByRole('button', { name: 'Recover this request' })
  expect(screen.getByLabelText('What made this moment memorable?')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Recover this request' }).closest('[role="status"]')).toHaveFocus()
  expect(screen.queryByRole('button', { name: 'Reload saved context' })).not.toBeInTheDocument()
  expect(analyze).toHaveBeenCalledOnce()
  const originalRequest = analyze.mock.calls[0][2]
  expect(originalRequest).toEqual({ revision: 1, ai: true, weather: false, audio_asset_id: null, request_id: expect.stringMatching(/^[0-9a-f-]{36}$/) })
  await click('Recover this request')
  expect(analyze).toHaveBeenLastCalledWith('place', 'capture', originalRequest)
  expect(save).toHaveBeenCalledOnce()
  expect(await screen.findByText(/Your earlier request was recovered. No new analysis/)).toBeVisible()
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('My own words')
  expect(screen.queryByRole('button', { name: 'Recover this request' })).not.toBeInTheDocument()
})

it.each(['superseded', 'refused'] as const)('loads current notes after a %s intent receipt without analyzing new settings', async (outcome) => {
  const value = context({ analysis_idempotency: true })
  const client = setup(value)
  const analyze = vi.spyOn(client, 'analyzeMemory').mockImplementation(async (_s, _c, request) => ({
    ...value, revision: 2,
    notes: { ...EMPTY_NOTES, description: 'A friend updated this story', location: { latitude: 30.27, longitude: -97.75 } },
    analysis: { id: 'newer-job', status: 'outdated', started_at: 2 },
    analysis_request: { id: request.request_id!, analysis_id: outcome === 'refused' ? null : 'old-job', rejection: outcome === 'refused' ? 'This memory changed.' : null, replayed: true, current: false },
  }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  await waitFor(() => expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('A friend updated this story'))
  expect(screen.getByLabelText('What made this moment memorable?')).not.toBeDisabled()
  expect(analyze).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: 'Recover this request' })).not.toBeInTheDocument()
})

it('keeps an unconfirmed response recoverable rather than treating missing acknowledgement as success', async () => {
  const value = context({ analysis_idempotency: true })
  const client = setup(value)
  const analyze = vi.spyOn(client, 'analyzeMemory').mockResolvedValue(value)
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  expect(await screen.findByRole('alert')).toHaveTextContent('did not confirm this request')
  expect(screen.getByRole('button', { name: 'Recover this request' })).toBeEnabled()
  expect(analyze).toHaveBeenCalledOnce()
})

it('does not adopt an old analysis reply or recovery reference after the client changes', async () => {
  const value = context({ analysis_idempotency: true })
  const first = setup(value)
  let resolve!: (value: MemoryContext) => void
  const analyze = vi.spyOn(first, 'analyzeMemory').mockReturnValue(new Promise(done => { resolve = done }))
  const view = render(<MemoryPanel client={first} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  const second = setup(context({ notes: { ...EMPTY_NOTES, description: 'Current account story' } }), 'second-account')
  const secondAnalyze = vi.spyOn(second, 'analyzeMemory')
  view.rerender(<MemoryPanel client={second} spaceId="place" capture={capture} />)
  await waitFor(() => expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('Current account story'))
  const request = analyze.mock.calls[0][2]
  await act(async () => resolve({
    ...value, notes: { ...EMPTY_NOTES, description: 'Old account story' },
    analysis: { id: 'old-job', status: 'complete', started_at: 1 },
    analysis_request: { id: request.request_id!, analysis_id: 'old-job', rejection: null, replayed: false, current: true },
  }))
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('Current account story')
  expect(screen.queryByRole('button', { name: 'Recover this request' })).not.toBeInTheDocument()
  expect(secondAnalyze).not.toHaveBeenCalled()
})

it('restores an uncertain request through a new client and panel without automatically sending it', async () => {
  const value = context({ analysis_idempotency: true })
  const first = setup(value)
  const initial = vi.spyOn(first, 'analyzeMemory').mockRejectedValue(new Error('Reply lost'))
  const view = render(<MemoryPanel client={first} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  await screen.findByRole('button', { name: 'Recover this request' })
  const request = initial.mock.calls[0][2]
  view.unmount()
  const second = setup(value)
  const recover = vi.spyOn(second, 'analyzeMemory').mockImplementation(async (_s, _c, body) => ({
    ...value, analysis: { id: 'job', status: 'complete', started_at: 1 },
    analysis_request: { id: body.request_id!, analysis_id: 'job', rejection: null, replayed: true, current: true },
  }))
  const done = vi.fn()
  render(<MemoryPanel client={second} spaceId="place" capture={capture} onDone={done} />)
  await screen.findByRole('button', { name: 'Recover this request' })
  expect(recover).not.toHaveBeenCalled()
  expect(screen.getByText(/Saved settings: AI on/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Done for now' })).toBeEnabled()
  await click('Recover this request')
  expect(recover).toHaveBeenCalledExactlyOnceWith('place', 'capture', request)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Recover this request' })).not.toBeInTheDocument())
  expect(await second.analysisRecovery.read('place', 'capture')).toBeNull()
})

it('does not send analysis when its recovery reference cannot be saved', async () => {
  const client = setup(context({ analysis_idempotency: true }))
  const analyze = vi.spyOn(client, 'analyzeMemory')
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('Storage full') })
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  expect(await screen.findByRole('alert')).toHaveTextContent('Storage full')
  expect(analyze).not.toHaveBeenCalled()
})

it('keeps originals editable when recovery cannot be read and only rechecks storage on request', async () => {
  const client = setup(context({ analysis_idempotency: true }))
  const read = vi.spyOn(client.analysisRecovery, 'read').mockRejectedValue(new Error('Storage blocked'))
  const analyze = vi.spyOn(client, 'analyzeMemory')
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await screen.findByText(/Analysis recovery is unavailable/)
  expect(screen.getByLabelText('What made this moment memorable?')).not.toBeDisabled()
  read.mockResolvedValue(null)
  await click('Check recovery storage')
  await waitFor(() => expect(screen.queryByText(/Analysis recovery is unavailable/)).not.toBeInTheDocument())
  expect(analyze).not.toHaveBeenCalled()
})

it('keeps the same identity recoverable when the reply arrives but clearing local storage fails', async () => {
  const value = context({ analysis_idempotency: true })
  const client = setup(value)
  const analyze = vi.spyOn(client, 'analyzeMemory').mockImplementation(async (_s, _c, body) => ({
    ...value, analysis: { id: 'job', status: 'complete', started_at: 1 },
    analysis_request: { id: body.request_id!, analysis_id: 'job', rejection: null, replayed: true, current: true },
  }))
  const remove = vi.spyOn(localStorage, 'removeItem').mockImplementation(() => { throw new Error('Storage blocked') })
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  await click(/Let the details come to you/)
  await userEvent.click(screen.getByLabelText(/Draft my memory with AI/))
  await click('Find the details')
  expect(await screen.findByRole('alert')).toHaveTextContent('reference could not be cleared')
  const pending = analyze.mock.calls[0][2]
  expect(await client.analysisRecovery.read('place', 'capture')).toEqual(pending)
  remove.mockRestore()
  await click('Recover this request')
  expect(analyze).toHaveBeenLastCalledWith('place', 'capture', pending)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Recover this request' })).not.toBeInTheDocument())
})

it('never silently discards unsaved text when another panel creates a recoverable intent', async () => {
  const value = context({ analysis_idempotency: true })
  const client = setup(value)
  const analyze = vi.spyOn(client, 'analyzeMemory').mockImplementation(async (_s, _c, body) => ({
    ...value, notes: { ...EMPTY_NOTES, description: 'Saved story' },
    analysis: { id: 'job', status: 'complete', started_at: 1 },
    analysis_request: { id: body.request_id!, analysis_id: 'job', rejection: null, replayed: true, current: true },
  }))
  render(<MemoryPanel client={client} spaceId="place" capture={capture} />)
  fireEvent.change(await screen.findByLabelText('What made this moment memorable?'), { target: { value: 'Keep my unsaved words' } })
  await act(async () => { await setup(value).analysisRecovery.reserve('place', 'capture', {
    revision: 0, ai: true, weather: false, audio_asset_id: null, request_id: crypto.randomUUID(),
  }) })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  await click('Recover this request')
  expect(analyze).not.toHaveBeenCalled()
  expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('Keep my unsaved words')
  confirm.mockReturnValue(true)
  await click('Recover this request')
  await waitFor(() => expect(screen.getByLabelText('What made this moment memorable?')).toHaveValue('Saved story'))
  expect(analyze).toHaveBeenCalledOnce()
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
  expect(await screen.findByText(/You can revisit this memory/)).toBeInTheDocument()
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
