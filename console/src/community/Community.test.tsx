import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { SpacesModule } from '../atlas/SpacesModule'
import { AtlasClient } from '../atlas/client'
import { WorkspaceHeader } from '../shell/WorkspaceHeader'
import { Rail } from '../shell/Rail'
import { AccountButton } from './AccountButton'
import { AccountSetup } from './AccountSetup'
import type { Space, SpaceDetail } from '../atlas/types'
import type { SpaceDraft, SpaceDraftStore } from '../atlas/drafts'

vi.mock('../atlas/SpaceMap', () => ({ default: ({ spaces, onSelect }: { spaces: Space[]; onSelect: (id: string) => void }) => <div aria-label="Map boundary">{spaces.map(space => <button key={space.id} onClick={() => onSelect(space.id)}>Map: {space.title}</button>)}</div> }))
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) })
  vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', '')
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); vi.unstubAllGlobals() })

const space: Space = { id: 'park', title: 'Our park', place: 'Mueller · Austin, Texas', description: 'A neighborhood view',
  category: 'community', latitude: 30.2973, longitude: -97.7054, radius: 120, created_at: 1, updated_at: 1,
  status: 'active', verification: 'unverified', capture_count: 0, contributors: 0, coverage_percent: 0 }

it('has one brand in the shared header and no repeated sidebar logo', () => {
  render(<><WorkspaceHeader onOpenFleet={vi.fn()} /><Rail modules={[]} active="spaces" onSelect={vi.fn()} /></>)
  expect(screen.getAllByText('sweep')).toHaveLength(1)
  expect(screen.getByText('Better, together.')).toBeInTheDocument()
  expect(screen.queryByText('FIELD WORKSPACE')).not.toBeInTheDocument()
})

it('offers clear, explicitly unconfigured sign-in instead of fake provider actions', () => {
  render(<AccountButton />)
  fireEvent.click(screen.getByRole('button', { name: 'Join in' }))
  expect(screen.getByText(/Social sign-in is not configured/)).toBeInTheDocument()
  expect(screen.getByLabelText('Planned sign-in providers')).toHaveTextContent('AppleGoogleX / Twitter')
  expect(screen.queryByRole('button', { name: 'Google' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Keep exploring' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('keeps native sign-in out of the embedded WebView even with a web key', () => {
  vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_not-a-real-key')
  render(<AccountSetup native />)
  fireEvent.click(screen.getByRole('button', { name: 'Join in' }))
  expect(screen.getByText(/Social sign-in for the Android app still needs native browser setup/)).toBeInTheDocument()
})

it('lets disconnected visitors search and explore clearly marked Austin examples without posting anything', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch')
  render(<SpacesModule services={{}} />)
  fireEvent.change(screen.getByRole('textbox', { name: 'Search spaces' }), { target: { value: 'Mueller' } })
  expect(screen.queryByRole('button', { name: /EXAMPLE.*Shoal Creek/ })).not.toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: 'Map: Example · A park for every neighbor' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('AUSTIN STARTER STORY · NOT A LIVE REPORT')).toBeInTheDocument()
  expect(within(dialog).getAllByRole('listitem')).toHaveLength(3)
  expect(within(dialog).getByRole('button', { name: 'Connect a workspace to start' })).toBeInTheDocument()
  expect(fetch).not.toHaveBeenCalled()
})

it('saves spaces across remounts without changing the relay or awarding points', async () => {
  const client = new AtlasClient({ baseUrl: 'https://workspace.test', sessionId: 'test', token: 'test' })
  vi.spyOn(client, 'list').mockResolvedValue([space])
  const create = vi.spyOn(client, 'create')
  const { unmount } = render(<SpacesModule services={{ atlas: client }} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Save Our park' }))
  fireEvent.click(screen.getByRole('button', { name: 'Saved' }))
  expect(screen.getByRole('heading', { name: 'Our park' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Your journey: 0 perspective points' })).toBeInTheDocument()
  unmount()
  render(<SpacesModule services={{ atlas: client }} />)
  expect(await screen.findByRole('button', { name: 'Unsave Our park' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Unsave Our park' }))
  fireEvent.click(screen.getByRole('button', { name: 'Saved' }))
  expect(screen.getByRole('heading', { name: 'A place to come back to.' })).toBeInTheDocument()
  expect(create).not.toHaveBeenCalled()
})

it('offers an actionable guide and honest reward rules', () => {
  render(<SpacesModule services={{}} />)
  fireEvent.click(screen.getByRole('button', { name: 'How Sweep works' }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Find your place')
  expect(screen.getByRole('dialog')).toHaveTextContent('never approach a dangerous situation')
  fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
  fireEvent.click(screen.getByRole('button', { name: 'Your journey: 0 perspective points' }))
  expect(screen.getByRole('dialog')).toHaveTextContent('don’t sync to your account yet')
  expect(screen.getByRole('progressbar')).toHaveAttribute('value', '0')
})

it('preserves an existing private draft when a different starter story is opened', async () => {
  const client = new AtlasClient({ baseUrl: 'https://workspace.test', sessionId: 'draft-test', token: 'test' })
  let saved: SpaceDraft | null = null
  const store: SpaceDraftStore = { read: vi.fn(async () => saved),
    write: vi.fn(async value => { saved = structuredClone(value) }), remove: vi.fn(async () => { saved = null }) }
  vi.spyOn(client, 'drafts', 'get').mockReturnValue(store)
  vi.spyOn(client, 'list').mockResolvedValue([])
  const publish = vi.spyOn(client, 'publishDraft')
  render(<SpacesModule services={{ atlas: client }} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Map: Example · A little love for Shoal Creek' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start a space like this' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Start a space like this' }))
  expect(screen.getByLabelText('Space name')).toHaveValue('A little love for Shoal Creek')
  expect(screen.getByLabelText('Latitude')).toHaveValue(30.2826)
  fireEvent.change(screen.getByLabelText('Space name'), { target: { value: 'Our own creek project' } })
  fireEvent.click(screen.getByRole('button', { name: 'Keep for later' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Map: Example · A park for every neighbor' }))
  fireEvent.click(screen.getByRole('button', { name: 'Continue your existing draft' }))
  expect(screen.getByLabelText('Space name')).toHaveValue('Our own creek project')
  expect(screen.getByLabelText('Latitude')).toHaveValue(30.2826)
  expect(screen.getByText(/Your existing draft is safe/)).toBeInTheDocument()
  expect(publish).not.toHaveBeenCalled()
})

it('records confirmed contributions in the UI and keeps progress isolated between workspaces', async () => {
  localStorage.setItem('sweep.atlas.contributor', 'neighbor')
  const detail: SpaceDetail = { space, captures: [{ id: 'original', contributor_id: 'neighbor', name: 'Taylor',
    kind: 'photo', source: 'import', captured_at: null, position: null, note: 'Public path',
    uploaded_at: 1, bytes: 20, mime: 'image/png', sha256: 'a'.repeat(64) }], people: [], requests: [],
    coverage: { cells: [], observed: 0, total: 0, percent: 0, qualified_captures: 0, meaning: 'Capture positions' },
    reconstruction: { status: 'idle', source_count: 1, detail: 'Add more views' } }
  const client = new AtlasClient({ baseUrl: 'https://workspace.test', sessionId: 'one', token: 'test' })
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  const first = render(<SpacesModule services={{ atlas: client }} initialSpace="park" />)
  fireEvent.click(await screen.findByRole('button', { name: 'Your journey: 10 perspective points' }))
  expect(within(screen.getByRole('dialog')).getByRole('heading', { name: 'First perspective' })).toBeInTheDocument()
  first.unmount()
  const other = new AtlasClient({ baseUrl: 'https://workspace.test', sessionId: 'two', token: 'test' })
  vi.spyOn(other, 'list').mockResolvedValue([])
  render(<SpacesModule services={{ atlas: other }} />)
  expect(screen.getByRole('button', { name: 'Your journey: 0 perspective points' })).toBeInTheDocument()
})

it('reports local storage failure without claiming a bookmark was saved', async () => {
  const client = new AtlasClient({ baseUrl: 'https://workspace.test', sessionId: 'test', token: 'test' })
  vi.spyOn(client, 'list').mockResolvedValue([space])
  render(<SpacesModule services={{ atlas: client }} />)
  const save = await screen.findByRole('button', { name: 'Save Our park' })
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('Storage full') })
  fireEvent.click(save)
  expect(screen.getByRole('status')).toHaveTextContent('Your progress could not be saved')
  expect(save).toHaveAttribute('aria-pressed', 'false')
})
