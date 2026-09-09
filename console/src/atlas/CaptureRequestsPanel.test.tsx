import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { CaptureRequestsPanel } from './CaptureRequestsPanel'
import type { SpaceDetail } from './types'

function fixture(): SpaceDetail {
  return {
    space: { id: 'austin', title: 'Creek survey', description: '', place: 'Austin', category: 'survey', latitude: 30.27,
      longitude: -97.74, radius: 80, status: 'active', verification: 'unverified', created_at: 1, updated_at: 1,
      capture_count: 1, contributors: 1, coverage_percent: 1 },
    captures: [], people: [], coverage: { cells: [], observed: 1, total: 80, percent: 1, qualified_captures: 1, meaning: 'Locations only' },
    requests: [{ cell_id: '5:5', latitude: 30.27, longitude: -97.74, note: 'View from the public path.', status: 'open', created_at: 2 },
      { cell_id: '6:5', latitude: 30.27, longitude: -97.74, note: 'Original location request.', status: 'captured', created_at: 1, capture_ids: ['original'] }],
    reconstruction: { id: 'current', status: 'ready', source_count: 11, detail: 'Built', artifact_sha256: 'a'.repeat(64),
      surface_review: { method: 'open-edges', boundary_edges: 3, nonmanifold_edges: 0,
        regions: [{ id: 'region', label: 'Creek edge', center: [0, 0, 0], radius: 1, boundary_edges: 3 }] } },
    surface_requests: [{ job_id: 'current', artifact_sha256: 'a'.repeat(64), region_id: 'region', label: 'Creek edge',
      note: 'Include the surrounding bank.', status: 'open', created_at: 3, updated_at: 3 },
    { job_id: 'old', artifact_sha256: 'b'.repeat(64), region_id: 'region', label: 'Earlier bank',
      note: 'Old request', status: 'open', created_at: 1, updated_at: 1, capture_ids: ['earlier'] }],
  }
}

it('combines active map and exact current-model requests with source-bound actions', () => {
  const detail = fixture(), onInspect = vi.fn(), onContribute = vi.fn(), onViewCaptures = vi.fn()
  render(<CaptureRequestsPanel {...{ detail, onInspect, onContribute, onViewCaptures }} />)
  expect(screen.getByRole('heading', { name: '2 views requested.' })).toBeInTheDocument()
  expect(screen.getAllByRole('article').map(item => item.getAttribute('aria-label'))).toEqual(['Creek edge', 'Map area 5:5'])
  expect(onInspect).not.toHaveBeenCalled()
  const surface = within(screen.getByRole('article', { name: 'Creek edge' }))
  fireEvent.click(surface.getByRole('button', { name: 'Inspect in 3D' }))
  expect(onInspect).toHaveBeenCalledWith(detail.surface_requests![0])
  fireEvent.click(surface.getByRole('button', { name: 'Contribute this view' }))
  expect(onContribute).toHaveBeenCalledWith({ target: { kind: 'surface', job_id: 'current', region_id: 'region', artifact_sha256: 'a'.repeat(64) }, label: 'Creek edge', note: 'Include the surrounding bank.' })
  fireEvent.click(screen.getByRole('button', { name: 'Show 2 previous requests' }))
  const old = within(screen.getByRole('article', { name: 'Earlier bank' }))
  expect(old.queryByRole('button', { name: 'Inspect in 3D' })).not.toBeInTheDocument()
  expect(old.queryByRole('button', { name: 'Contribute this view' })).not.toBeInTheDocument()
  fireEvent.click(old.getByRole('button', { name: 'View 1 linked capture' }))
  expect(onViewCaptures).toHaveBeenCalledWith(expect.objectContaining({ target: { kind: 'surface', job_id: 'old', region_id: 'region', artifact_sha256: 'b'.repeat(64) } }))
})

it('polling retires captured, dismissed, old-checksum and resolved targets without deleting history', () => {
  const detail = fixture(), props = { onInspect: vi.fn(), onContribute: vi.fn(), onViewCaptures: vi.fn() }
  const view = render(<CaptureRequestsPanel detail={detail} {...props} />)
  const updated = structuredClone(detail)
  updated.requests[0].status = 'captured'
  updated.surface_requests![0].artifact_sha256 = 'c'.repeat(64)
  view.rerender(<CaptureRequestsPanel detail={updated} {...props} />)
  expect(screen.getByRole('heading', { name: 'No open requests.' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Contribute this view' })).not.toBeInTheDocument()
  updated.surface_requests![0].status = 'dismissed'
  view.rerender(<CaptureRequestsPanel detail={updated} {...props} />)
  expect(screen.getByRole('button', { name: 'Show 4 previous requests' })).toBeInTheDocument()
  view.rerender(<CaptureRequestsPanel detail={{ ...detail, space: { ...detail.space, status: 'resolved' } }} {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Show 4 previous requests' }))
  expect(screen.getAllByRole('article')).toHaveLength(4)
  expect(screen.queryByRole('button', { name: 'Contribute this view' })).not.toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'View 1 linked capture' })).toHaveLength(2)
})
