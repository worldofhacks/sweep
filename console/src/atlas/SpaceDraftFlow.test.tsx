import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SpacesModule } from './SpacesModule'
import { AtlasClient } from './client'
import type { SpaceDraft, SpaceDraftStore } from './drafts'
import type { SpaceDetail } from './types'

vi.mock('./SpaceMap', () => ({ default: () => <div aria-label="Geographic map test boundary" /> }))
afterEach(() => vi.restoreAllMocks())
function setup() {
  let saved: SpaceDraft | null = null
  const store: SpaceDraftStore = {
    read: vi.fn(async () => saved), write: vi.fn(async value => { saved = structuredClone(value) }),
    remove: vi.fn(async () => { saved = null }),
  }
  const client = new AtlasClient({ baseUrl: 'https://relay.example', sessionId: 'Austin', token: 'owner-key' })
  vi.spyOn(client, 'drafts', 'get').mockReturnValue(store)
  vi.spyOn(client, 'list').mockRejectedValue(new Error('Workspace offline.'))
  const publish = vi.spyOn(client, 'publishDraft')
  const detail: SpaceDetail = { space: { id: 'published', title: 'Austin access survey', description: 'Observe the creek crossing.',
    place: 'Shoal Creek, Austin', category: 'survey', latitude: 30.279, longitude: -97.748, radius: 80,
    created_at: Date.now(), updated_at: Date.now(), status: 'active', verification: 'unverified', contributors: 0,
    capture_count: 0, coverage_percent: 0 }, captures: [], requests: [], people: [],
    coverage: { cells: [], percent: 0, observed: 0, total: 0, qualified_captures: 0, meaning: 'Camera locations' },
    reconstruction: { status: 'idle', source_count: 0, detail: 'Add views' } }
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  return { client, store, publish, detail, read: () => saved }
}
async function fillDraft() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create a space' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Create a space' }))
  fireEvent.change(screen.getByLabelText('Space name'), { target: { value: 'Austin access survey' } })
  fireEvent.change(screen.getByLabelText('The story so far'), { target: { value: 'Observe the creek crossing.' } })
  fireEvent.change(screen.getByLabelText('Place name'), { target: { value: 'Shoal Creek, Austin' } })
  fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '30.279' } })
  fireEvent.change(screen.getByLabelText('Longitude'), { target: { value: '-97.748' } })
  await screen.findByText('Draft saved on this device')
}
it('keeps offline text and location across remount and navigation without publishing', async () => {
  const { client, publish, read } = setup()
  const first = render(<SpacesModule services={{ atlas: client }} />)
  await fillDraft()
  fireEvent.click(screen.getByRole('button', { name: 'Keep for later' }))
  await screen.findByText('A private draft, ready when you are. Nothing has been shared.')
  expect(read()?.space).toMatchObject({ title: 'Austin access survey', latitude: 30.279, longitude: -97.748 })
  first.unmount()
  render(<SpacesModule services={{ atlas: client }} />)
  fireEvent.click((await screen.findAllByRole('button', { name: 'Continue draft' }))[0])
  expect(screen.getByLabelText('The story so far')).toHaveValue('Observe the creek crossing.')
  expect(screen.getByLabelText('Latitude')).toHaveValue(30.279)
  expect(publish).not.toHaveBeenCalled()
})
it('locks an uncertain publication, reopens it, and retries the identical payload and draft key', async () => {
  const { client, publish, detail, read } = setup()
  publish.mockRejectedValueOnce(new Error('Reply lost after publication.')).mockResolvedValueOnce({ space: detail.space, contributor_token: null })
  const first = render(<SpacesModule services={{ atlas: client }} />)
  await fillDraft()
  fireEvent.click(screen.getByRole('button', { name: 'Publish space' }))
  await screen.findByText('Reply lost after publication.')
  const original = read()!
  expect(original.submitted).not.toBeNull()
  expect(screen.getByLabelText('Space name')).toBeDisabled()
  first.unmount()
  render(<SpacesModule services={{ atlas: client }} />)
  fireEvent.click((await screen.findAllByRole('button', { name: 'Continue draft' }))[0])
  vi.mocked(client.list).mockResolvedValue([detail.space])
  fireEvent.click(screen.getByRole('button', { name: 'Check publication' }))
  await screen.findByRole('heading', { name: detail.space.title })
  expect(publish.mock.calls).toEqual([[original.id, original.submitted], [original.id, original.submitted]])
  expect(read()).toBeNull()
  expect(await screen.findByText('Your space is ready. Add the first perspective.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create a space' }))
  expect(screen.queryByText('Your space is ready. Add the first perspective.')).not.toBeInTheDocument()
  expect(document.querySelector('.atlas-toast')).toBeNull()
})
it('never reads a workspace draft for a scoped contributor invitation', async () => {
  const { client, store } = setup()
  render(<SpacesModule services={{ atlas: client }} initialSpace="published" />)
  await screen.findByRole('heading', { name: 'Austin access survey' })
  expect(screen.getByRole('button', { name: 'Create a space' })).toBeDisabled()
  expect(store.read).not.toHaveBeenCalled()
})
it('preserves edits and refuses publication when local persistence fails', async () => {
  const { client, store, publish } = setup()
  vi.mocked(store.write).mockRejectedValue(new Error('Device storage full'))
  render(<SpacesModule services={{ atlas: client }} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create a space' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Create a space' }))
  fireEvent.change(screen.getByLabelText('Space name'), { target: { value: 'Keep this report' } })
  fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '30.279' } })
  fireEvent.change(screen.getByLabelText('Longitude'), { target: { value: '-97.748' } })
  await screen.findByText('Draft not saved')
  fireEvent.click(screen.getByRole('button', { name: 'Publish space' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Publish space' })).toBeEnabled())
  expect(publish).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Space name')).toHaveValue('Keep this report')
  expect(document.querySelector('.atlas-draft-notice')).toHaveTextContent('Device storage full')
  expect(document.querySelector('.atlas-toast')).toBeNull()
})
