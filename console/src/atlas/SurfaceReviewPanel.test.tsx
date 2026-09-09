import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AtlasClient } from './client'
import { SurfaceReviewPanel } from './SurfaceReviewPanel'
import type { Reconstruction, SurfaceRegion, SurfaceRequest } from './types'

const region: SurfaceRegion = { id: '0123456789abcdef', label: 'Region 1', center: [1, 2, 3], radius: 2,
  boundary_edges: 10, segments: [[0, 0, 0], [1, 0, 0]] }

const job: Reconstruction = { id: 'build-one', status: 'ready', source_count: 11,
  detail: 'Built', artifact_sha256: 'a'.repeat(64), representation: 'textured_mesh',
  surface_review: { method: 'open-mesh-edges-v1', boundary_edges: 40, nonmanifold_edges: 0,
    candidate_regions: 2, regions: [region] } }
const request: SurfaceRequest = { job_id: job.id!, artifact_sha256: job.artifact_sha256!,
  region_id: '0123456789abcdef', label: 'Region 1', note: 'Add the context around the edge.',
  status: 'open', created_at: 1, updated_at: 1 }
const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'test', token: 'test-only' })
afterEach(() => vi.restoreAllMocks())
beforeEach(() => { vi.spyOn(client, 'surfaceRegion').mockResolvedValue(region) })

test('a request deep-link loads once across polling and retires in-flight geometry on leaving', async () => {
  let resolve!: (region: SurfaceRegion) => void
  const read = vi.mocked(client.surfaceRegion).mockImplementation(() => new Promise(done => { resolve = done }))
  const onFocus = vi.fn(), props = { job, requests: [request], client, spaceId: 'space', active: true, canManage: false,
    onFocus, onChange: vi.fn(), initialRegionId: region.id }
  const view = render(<SurfaceReviewPanel {...props} />)
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
  view.rerender(<SurfaceReviewPanel {...props} job={structuredClone(job)} requests={[{ ...request }]} />)
  expect(read).toHaveBeenCalledTimes(1)
  const signal = read.mock.calls[0][4]!
  view.unmount()
  expect(signal.aborted).toBe(true)
  resolve(region)
  await Promise.resolve()
  expect(onFocus).not.toHaveBeenCalled()
})

test('an initial region load failure can be retried explicitly', async () => {
  vi.mocked(client.surfaceRegion).mockRejectedValueOnce(new Error('Connection interrupted.')).mockResolvedValue(region)
  const onFocus = vi.fn()
  render(<SurfaceReviewPanel job={job} requests={[request]} client={client} spaceId="space" active canManage={false}
    onFocus={onFocus} onChange={() => {}} initialRegionId={region.id} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection interrupted.')
  fireEvent.click(screen.getByRole('button', { name: 'Region 1' }))
  await waitFor(() => expect(onFocus).toHaveBeenLastCalledWith({ job_id: job.id, artifact_sha256: job.artifact_sha256, region }))
  expect(client.surfaceRegion).toHaveBeenCalledTimes(2)
})

test('a contribution carries the exact build while dismissed and historical links remain inspectable', () => {
  const onContribute = vi.fn(), onViewCaptures = vi.fn()
  const props = { job, requests: [{ ...request, capture_ids: ['original'] }], client, spaceId: 'space', active: true, canManage: false,
    onFocus: vi.fn(), onChange: vi.fn(), onContribute, onViewCaptures }
  const view = render(<SurfaceReviewPanel {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Contribute this view' }))
  expect(onContribute).toHaveBeenCalledWith({ label: request.label, note: request.note,
    target: { kind: 'surface', job_id: request.job_id, region_id: request.region_id, artifact_sha256: request.artifact_sha256 } })
  fireEvent.click(screen.getByRole('button', { name: 'View 1 linked capture' }))
  expect(onViewCaptures).toHaveBeenCalledWith(onContribute.mock.calls[0][0])
  view.rerender(<SurfaceReviewPanel {...props} active={false} />)
  expect(screen.getByRole('button', { name: 'Contribute this view' })).toBeDisabled()
  view.rerender(<SurfaceReviewPanel {...props} job={{ ...job, id: 'new-job' }} requests={[{ ...request, status: 'dismissed', capture_ids: ['original'] }]} />)
  expect(screen.queryByRole('button', { name: 'Contribute this view' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'View 1 linked capture' })).toBeEnabled()
})

test('inspection has no mutation; explicit owner request binds to the exact displayed artifact', async () => {
  const onFocus = vi.fn(), onChange = vi.fn()
  const publish = vi.spyOn(client, 'requestSurface').mockResolvedValue(request)
  render(<SurfaceReviewPanel job={job} requests={[]} client={client} spaceId="space" active canManage
    onFocus={onFocus} onChange={onChange} />)
  fireEvent.click(screen.getByRole('button', { name: 'Region 1' }))
  expect(publish).not.toHaveBeenCalled()
  await waitFor(() => expect(onFocus).toHaveBeenCalledWith({ job_id: job.id, artifact_sha256: job.artifact_sha256, region }))
  fireEvent.change(screen.getByRole('textbox', { name: 'What would help?' }), { target: { value: '  Add overlapping views of this edge.  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Request more views' }))
  await screen.findByText('Request shared with contributors in this space.')
  expect(publish).toHaveBeenCalledExactlyOnceWith('space', onFocus.mock.calls[1][0], 'Add overlapping views of this edge.')
  expect(onChange).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Show whole model' }))
  expect(onFocus).toHaveBeenLastCalledWith(null)
})

test('contributor sees and inspects shared requests without owner mutation controls', async () => {
  const onFocus = vi.fn()
  render(<SurfaceReviewPanel job={job} requests={[request]} client={client} spaceId="space" active canManage={false}
    onFocus={onFocus} onChange={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Inspect requested region' }))
  await waitFor(() => expect(onFocus).toHaveBeenCalledTimes(2))
  expect(screen.queryByRole('button', { name: 'Request more views' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Dismiss request' })).not.toBeInTheDocument()
  expect(screen.getByText(/New captures do not automatically close this request/)).toBeInTheDocument()
})

test('previous-build requests remain visible but cannot focus the new geometry', async () => {
  const dismiss = vi.spyOn(client, 'dismissSurfaceRequest').mockResolvedValue({ ...request, status: 'dismissed' })
  const onFocus = vi.fn()
  render(<SurfaceReviewPanel job={{ ...job, id: 'next-build' }} requests={[request]} client={client}
    spaceId="space" active canManage onFocus={onFocus} onChange={() => {}} />)
  expect(screen.getByText(/Its highlight cannot be applied to the current model/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Inspect requested region' })).not.toBeInTheDocument()
  expect(onFocus).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss request' }))
  await screen.findByText('Request dismissed; no surface completeness was asserted.')
  expect(dismiss).toHaveBeenCalledExactlyOnceWith('space', 'build-one', request.region_id)
})

test('failed publishing remains actionable and resolved spaces disable new requests', async () => {
  const publish = vi.spyOn(client, 'requestSurface').mockRejectedValueOnce(new Error('The build changed. Refresh.')).mockResolvedValue(request)
  const props = { job, requests: [], client, spaceId: 'space', active: true, canManage: true, onFocus: vi.fn(), onChange: vi.fn() }
  const view = render(<SurfaceReviewPanel {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Region 1' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Request more views' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Request more views' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The build changed. Refresh.')
  expect(props.onChange).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Request more views' }))
  await waitFor(() => expect(props.onChange).toHaveBeenCalledTimes(1))
  expect(publish).toHaveBeenCalledTimes(2)
  view.rerender(<SurfaceReviewPanel {...props} active={false} />)
  expect(screen.getByRole('button', { name: 'Request more views' })).toBeDisabled()
})

test('no detected edge never claims a complete model', () => {
  render(<SurfaceReviewPanel job={{ ...job, surface_review: { ...job.surface_review!, regions: [] } }} requests={[]}
    client={client} spaceId="space" active canManage onFocus={() => {}} onChange={() => {}} />)
  expect(screen.getByText(/This does not prove the scene is complete/)).toBeInTheDocument()
})

test('leaving inspection cancels its manifest read and a late response cannot refocus the model', async () => {
  let resolve!: (region: SurfaceRegion) => void
  const read = vi.mocked(client.surfaceRegion).mockImplementation(() => new Promise(done => { resolve = done }))
  const onFocus = vi.fn()
  render(<SurfaceReviewPanel job={job} requests={[]} client={client} spaceId="space" active canManage onFocus={onFocus} onChange={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Region 1' }))
  expect(screen.getByRole('button', { name: 'Request more views' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Show whole model' }))
  expect(read.mock.calls[0][4].aborted).toBe(true)
  resolve(region)
  await waitFor(() => expect(onFocus.mock.calls).toEqual([[null], [null]]))
})
