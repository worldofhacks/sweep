import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AtlasClient } from './client'
import { SpacesModule } from './SpacesModule'
import type { SpaceDetail } from './types'

vi.mock('./SpaceMap', () => ({ default: () => <div aria-label="Geographic map test boundary" /> }))
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
