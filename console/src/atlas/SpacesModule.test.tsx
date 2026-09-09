import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AtlasClient } from './client'
import { SpacesModule, WorkspaceSpaces } from './SpacesModule'
import type { SpaceDetail, SurfaceFocus, SurfaceRegion } from './types'
import { EMPTY_NOTES } from '../memory/types'

vi.mock('./SpaceMap', () => ({ default: ({ center }: { center: [number, number] }) => <div aria-label="Geographic map test boundary">{center.join(',')}</div> }))
vi.mock('./WorldViewer', () => ({ default: ({ focus }: { focus?: SurfaceFocus | null }) =>
  <div aria-label="Model view test boundary">{focus?.region.label ?? 'Whole model'}</div> }))
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

function fixture() {
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'workspace', token: 'test-only' })
  const detail: SpaceDetail = {
    space: { id: 'place', title: 'Creek restoration', place: 'Creek path', description: 'Shared documentation',
      category: 'community', latitude: 37, longitude: -122, radius: 80, created_at: 1, updated_at: 1,
      status: 'active', verification: 'unverified', capture_count: 0, coverage_percent: 0, contributors: 1 },
    captures: [], requests: [], reconstruction: { status: 'idle', source_count: 0, detail: 'Add views' },
    coverage: { cells: [], observed: 0, total: 0, percent: 0, qualified_captures: 0, meaning: 'Capture positions, not surfaces' },
    people: [{ contributor_id: 'person-one', name: 'Maya', updated_at: Date.now(),
      position: { latitude: 37, longitude: -122, accuracy: 5, timestamp: Date.now() } }],
  }
  return { client, detail }
}

it('keeps confirmation independent of capture polling and opens receipts after withdrawal', async () => {
  const { client, detail } = fixture()
  const showModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', '') } })
  const capture = { id: 'source', name: 'Removal test contributor', contributor_id: 'acct_source', kind: 'photo' as const, source: 'import' as const, captured_at: null, uploaded_at: 1788950000000, position: null, note: '', mime: 'image/png', bytes: 10_000_000, sha256: 'checksum' }
  detail.captures = [capture]
  const read = vi.spyOn(client, 'detail').mockResolvedValue(detail)
  vi.spyOn(client, 'media').mockRejectedValue(new Error('No media fetch needed for this test'))
  vi.spyOn(client, 'memory').mockResolvedValue({ revision: 0, capture, notes: { ...EMPTY_NOTES }, assets: [], inspection: null, analysis: null, can_edit: true, can_remove: true, can_analyze: false, capabilities: { metadata: true, ai: false, weather: false, media_tools: false } })
  vi.spyOn(client, 'removal').mockResolvedValue({ capture_id: 'source', state: 'preview', confirmation: 'a'.repeat(64), recordings: 0, builds: 0, analysis_pending: false })
  let finish!: () => void
  const receipt = { capture_id: 'source', state: 'cleanup_pending' as const, recordings: 0, builds: 0, requested_by: 'workspace-operator', requested_at: 1788950000000, completed_at: null, analysis_pending: false }
  const remove = vi.spyOn(client, 'removeCapture').mockImplementation(() => new Promise(resolve => { finish = () => resolve(receipt) }))
  vi.spyOn(client, 'removals').mockResolvedValue({ receipts: [receipt], pending: 1, completed: 0, scope: 'space', next_before: null })
  const view = render(<WorkspaceSpaces services={{ atlas: client }} initialSpace="place" />)
  try {
    fireEvent.click(await screen.findByRole('button', { name: 'Captures' }))
    fireEvent.click(screen.getByRole('button', { name: 'Memory & sounds' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Details & sources' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review removal from this Space' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: /I understand this withdraws/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove from this Space' }))
    expect(remove).toHaveBeenCalledOnce()
    expect(screen.getByRole('button', { name: 'Back to memory' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
    expect(screen.getByRole('heading', { name: 'Remove a memory' })).toBeInTheDocument()
    // A normal Space refresh can remove the gallery card while its request is pending.
    read.mockResolvedValue({ ...detail, captures: [] })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2), { timeout: 6500 })
    expect(screen.getByRole('heading', { name: 'Remove a memory' })).toBeInTheDocument()
    await act(async () => finish())
    expect(await screen.findByRole('heading', { name: 'Removal status' })).toBeInTheDocument()
    expect(await screen.findByText('Access withdrawn · cleanup pending')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Memory & sounds' })).not.toBeInTheDocument())
  } finally {
    view.unmount()
    if (showModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', showModal)
    else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
  }
}, 10_000)

it('keeps account viewers on the existing readable surface without contribution controls', async () => {
  const { client, detail } = fixture()
  detail.requests = [{ cell_id: '5:5', note: 'A view would help.', latitude: 30.27, longitude: -97.74, created_at: 1, status: 'open' }]
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  render(<WorkspaceSpaces services={{ atlas: client }} initialSpace="place" accountRole="viewer" accountKey="user-viewer" />)
  expect(await screen.findByRole('button', { name: /Add a capture/ })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Share my location in this space' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Share space' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Requests' }))
  expect(screen.queryByRole('button', { name: 'Contribute this view' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Coverage' }))
  expect(screen.getByRole('button', { name: 'Select a missing area' })).toBeDisabled()
})

it('lets account owners share and manage their Space without an operator connection', async () => {
  const { client, detail } = fixture()
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  const status = vi.spyOn(client, 'status').mockResolvedValue({})
  render(<WorkspaceSpaces services={{ atlas: client }} initialSpace="place" accountRole="owner" accountKey="user-owner" />)
  expect(await screen.findByRole('button', { name: /Add a capture/ })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Share space' })).toBeEnabled()
  expect(screen.getByText(/This is your Space/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Mark this space resolved' }))
  expect(status).toHaveBeenCalledWith('place', 'resolved')
})

it('opens native capture for a requested cell without inventing GPS and filters linked originals before paging', async () => {
  const { client, detail } = fixture()
  detail.requests = [{ cell_id: '5:5', note: 'View the creek from the public path.', latitude: 30.2672, longitude: -97.7431, created_at: 1, status: 'open', capture_ids: ['original-29'] }]
  detail.captures = Array.from({ length: 30 }, (_, i) => ({ id: `original-${i}`, name: `Photographer ${i}`, contributor_id: 'person-one', source: 'import', kind: 'photo', captured_at: null, position: null, note: '', bytes: 10_000_000, mime: 'image/png', uploaded_at: 1, sha256: 'a'.repeat(64) }))
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  const captureNative = vi.fn().mockResolvedValue(undefined)
  render(<SpacesModule services={{ atlas: client }} initialSpace="place" captureNative={captureNative} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Requests' }))
  fireEvent.click(screen.getByRole('button', { name: 'Contribute this view' }))
  expect(captureNative).toHaveBeenCalledWith(expect.objectContaining({ spaceId: 'place', request: {
    target: { kind: 'location', cell_id: '5:5' }, label: 'Requested viewpoint', note: detail.requests[0].note,
  } }))
  expect(captureNative.mock.calls[0][0]).not.toHaveProperty('position')
  fireEvent.click(screen.getByRole('button', { name: 'View 1 linked capture' }))
  expect(screen.getByText('Photographer 29')).toBeInTheDocument()
  expect(screen.queryByText('Photographer 0')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Show all captures' }))
  expect(screen.getByText('Photographer 0')).toBeInTheDocument()
  expect(screen.queryByText('Photographer 29')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Show 6 more captures' }))
  expect(screen.getByText('Photographer 29')).toBeInTheDocument()
})

it('Needs views discovers actual requests even at 100% GPS coverage and opens their list', async () => {
  const { client, detail } = fixture()
  detail.space.open_request_count = 1
  detail.space.coverage_percent = 100
  detail.requests = [{ cell_id: '5:5', latitude: 30.27, longitude: -97.74, status: 'open', created_at: 1, note: 'Public path' }]
  detail.coverage.cells = [{ id: '5:5', captures: 0, latitude: 30.27, longitude: -97.74, x: 0, y: 0, size: 16 }]
  vi.spyOn(client, 'list').mockResolvedValue([detail.space,
    { ...detail.space, id: 'empty', title: 'No request yet', coverage_percent: 0, open_request_count: 0 }])
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  const request = vi.spyOn(client, 'request'), upload = vi.spyOn(client, 'upload')
  render(<SpacesModule services={{ atlas: client }} />)
  await screen.findByRole('heading', { name: 'No request yet' })
  fireEvent.click(screen.getByRole('button', { name: 'Needs views' }))
  expect(screen.queryByRole('heading', { name: 'No request yet' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Open Creek restoration' }))
  const panel = await screen.findByRole('region', { name: 'Capture requests' })
  expect(within(panel).getByRole('heading', { name: '1 view requested.' })).toBeInTheDocument()
  fireEvent.click(within(panel).getByRole('button', { name: 'Show requested area' }))
  expect(screen.getByRole('button', { name: 'Coverage' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByLabelText('Geographic map test boundary')).toHaveTextContent('-97.74,30.27')
  expect(screen.getByRole('button', { name: 'Area 5:5, 0 captures' })).toHaveAttribute('aria-pressed', 'true')
  expect(request).not.toHaveBeenCalled()
  expect(upload).not.toHaveBeenCalled()
})

it('a request opens the exact 3D region and normal return to 3D clears it', async () => {
  const { client, detail } = fixture()
  const region: SurfaceRegion = { id: '0123456789abcdef', label: 'Region 1', center: [1, 2, 3],
    radius: 1, boundary_edges: 3, segments: [[0, 0, 0], [1, 0, 0]] }
  detail.reconstruction = { id: 'build', status: 'ready', artifact_sha256: 'a'.repeat(64), source_count: 11, detail: 'Built',
    surface_review: { method: 'open-mesh-edges-v1', boundary_edges: 3, nonmanifold_edges: 0, regions: [region] } }
  detail.surface_requests = [{ job_id: 'build', artifact_sha256: 'a'.repeat(64), region_id: region.id,
    label: region.label, note: 'Include surrounding detail', status: 'open', created_at: 1, updated_at: 1 }]
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  const read = vi.spyOn(client, 'surfaceRegion').mockResolvedValue(region)
  render(<SpacesModule services={{ atlas: client }} initialSpace="place" />)
  fireEvent.click(await screen.findByRole('button', { name: /1 view requested/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Inspect in 3D' }))
  expect(await screen.findByText('Region 1', { selector: '[aria-label="Model view test boundary"]' })).toBeInTheDocument()
  expect(read).toHaveBeenCalledExactlyOnceWith('place', 'build', 'a'.repeat(64), region.id, expect.any(AbortSignal))
  expect(screen.getByRole('button', { name: 'Region 1' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
  fireEvent.click(screen.getByRole('button', { name: '3D atlas' }))
  expect(await screen.findByText('Whole model')).toBeInTheDocument()
  expect(read).toHaveBeenCalledTimes(1)
})

it('the request filter explains no matches and returns to Explore without suggesting a nonexistent first space', async () => {
  const { client, detail } = fixture()
  vi.spyOn(client, 'list').mockResolvedValue([detail.space])
  render(<SpacesModule services={{ atlas: client }} />)
  await screen.findByRole('heading', { name: 'Creek restoration' })
  fireEvent.click(screen.getByRole('button', { name: 'Needs views' }))
  expect(screen.getByRole('heading', { name: 'No open requests right now.' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Explore all spaces' }))
  expect(screen.getByRole('heading', { name: 'Creek restoration' })).toBeInTheDocument()
})

it('shows an actionable failure instead of an indefinite loading state and permits retry', async () => {
  const { client, detail } = fixture()
  const read = vi.spyOn(client, 'detail').mockRejectedValueOnce(new Error('Invitation expired')).mockResolvedValue(detail)
  render(<SpacesModule services={{ atlas: client }} initialSpace="place" />)
  expect(await screen.findByText('Space unavailable')).toBeInTheDocument()
  expect(screen.getByText('Invitation expired')).toBeInTheDocument()
  expect(screen.queryByText('Loading this space…')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByRole('heading', { name: 'Creek restoration' })).toBeInTheDocument()
  expect(read).toHaveBeenCalledTimes(2)
  expect(screen.queryByText('Space unavailable')).not.toBeInTheDocument()
})

it('removes stale details and live people when a later detail poll is refused', async () => {
  vi.useFakeTimers()
  const { client, detail } = fixture()
  vi.spyOn(client, 'detail').mockResolvedValueOnce(detail).mockRejectedValue(new Error('Access revoked'))
  await act(async () => { render(<SpacesModule services={{ atlas: client }} initialSpace="place" />) })
  expect(screen.getByText('Maya')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByText('Access revoked')).toBeInTheDocument()
  expect(screen.queryByText('Maya')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Creek restoration' })).not.toBeInTheDocument()
})

it('returning to 3D clears an old review highlight and never publishes a request on navigation', async () => {
  const { client, detail } = fixture()
  const region: SurfaceRegion = { id: '0123456789abcdef', label: 'Region 1', center: [1, 2, 3],
    radius: 1, boundary_edges: 3, segments: [[0, 0, 0], [1, 0, 0]] }
  detail.reconstruction = { status: 'ready', id: 'job', artifact_sha256: 'a'.repeat(64), source_count: 11,
    detail: 'Built', surface_review: { method: 'open-mesh-edges-v1', boundary_edges: 3, nonmanifold_edges: 0, regions: [region] } }
  vi.spyOn(client, 'detail').mockResolvedValue(detail)
  vi.spyOn(client, 'surfaceRegion').mockResolvedValue(region)
  const publish = vi.spyOn(client, 'requestSurface')
  render(<SpacesModule services={{ atlas: client }} initialSpace="place" />)
  fireEvent.click(await screen.findByRole('button', { name: '3D atlas' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Region 1' }))
  expect(await screen.findByText('Region 1', { selector: '[aria-label="Model view test boundary"]' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Coverage' }))
  fireEvent.click(screen.getByRole('button', { name: '3D atlas' }))
  expect(await screen.findByText('Whole model')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Region 1' })).toHaveAttribute('aria-pressed', 'false')
  expect(publish).not.toHaveBeenCalled()
})
