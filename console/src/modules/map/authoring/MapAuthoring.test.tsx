import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { MapAuthoring } from './MapAuthoring'
import { clientFixture, draftFixture, revision } from './test-fixtures'
import { loadOccupancyImage } from './files'
import type { MapDraft } from './types'

vi.mock('./files', async (load) => ({ ...await load<typeof import('./files')>(), loadOccupancyImage: vi.fn(), verifyDraftImage: vi.fn(async (draft: MapDraft) => draft) }))

test('default operator UI contains no generated map and every relay action explains unavailability', () => {
  render(<MapAuthoring />)
  expect(screen.getByText('Load an actual occupancy image. No map or device positions are generated.')).toBeInTheDocument()
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByText(/Relay map authoring is unavailable/)).toBeInTheDocument()
  for (const name of ['Load revision list', 'Save to relay', 'Validate saved revision', 'Review approval']) {
    expect(screen.getByRole('button', { name })).toBeDisabled()
  }
  expect(screen.getByRole('combobox', { name: 'Distance units' })).toHaveValue('')
})

test('operator-loaded image requires real metadata before drawing and supports edit, delete, and undo', async () => {
  const user = userEvent.setup()
  vi.mocked(loadOccupancyImage).mockResolvedValue(draftFixture().image!)
  render(<MapAuthoring />)
  await user.upload(screen.getByLabelText('Load actual occupancy image'), new File(['isolated test bytes'], 'test-map.png', { type: 'image/png' }))
  await screen.findByAltText('Operator-loaded occupancy image; coordinates not yet configured')
  expect(screen.getByRole('button', { name: 'Draw geofence' })).toBeDisabled()
  for (const [label, value] of [['Map version', 'test-map-v1'], ['Floor ID', 'test-floor'], ['Coordinate frame', 'world'], ['Resolution · metres per pixel', '0.1'], ['Bottom-left origin x · m', '0'], ['Bottom-left origin y · m', '0']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  await user.selectOptions(screen.getByRole('combobox', { name: 'Distance units' }), 'm')
  for (const [label, value] of [['Map created at · UTC', '1970-01-01T00:00:01'], ['Map creation evidence', 'Isolated test source'], ['Original image frame', 'test-map'], ['Registration identity', 'test-registration'], ['Registration measurement evidence', 'Isolated test registration'], ['Measured registration residual · m', '0.01'], ['Accepted registration threshold · m', '0.02']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  const canvas = screen.getByRole('img', { name: 'Map authoring canvas' })
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100, toJSON: () => ({}) })
  await user.click(screen.getByRole('button', { name: 'Draw geofence' }))
  for (const [clientX, clientY] of [[0, 100], [100, 100], [100, 0], [0, 0]]) fireEvent.pointerDown(canvas, { button: 0, pointerId: 1, clientX, clientY })
  await user.click(screen.getByRole('button', { name: 'Finish geometry' }))
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Measured boundary' } })
  expect(screen.getByText('Local checks · passed')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Delete feature' }))
  expect(screen.getByText(/Exactly one geofence is required/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Undo edit' }))
  expect(within(screen.getByRole('group', { name: 'Draft objects' })).getByRole('button', { name: 'Measured boundary' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Review approval' })).toBeDisabled()
})

test('relay approval requires an explicit review of the exact saved hash and validation', async () => {
  const user = userEvent.setup(), client = clientFixture()
  render(<MapAuthoring client={client} now={() => 10_000} />)
  await user.click(screen.getByRole('button', { name: 'Load revision list' }))
  await user.click(await screen.findByRole('button', { name: 'Load revision-1' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Validate saved revision' })).toBeEnabled())
  expect(client.approve).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Validate saved revision' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review approval' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Review approval' }))
  const confirmation = screen.getByRole('group', { name: 'Confirm exact map approval' })
  expect(within(confirmation).getByText(revision.contentHash)).toBeInTheDocument()
  expect(client.approve).not.toHaveBeenCalled()
  await user.click(within(confirmation).getByRole('button', { name: 'Approve exact saved revision' }))
  await waitFor(() => expect(client.approve).toHaveBeenCalledWith(revision, 'test-validation'))
  expect(await screen.findByText('Relay-approved revision')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Map version'), { target: { value: 'edited-after-approval' } })
  expect(screen.queryByText('Relay-approved revision')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Review approval' })).toBeDisabled()
})

test('corridor inspector never derives flight height or evidence from the occupancy raster', async () => {
  const user = userEvent.setup(), client = clientFixture()
  const draft = draftFixture()
  draft.features.push({ ...draft.features[0], id: 'corridor', kind: 'corridor', name: 'Test route', points: [{ x: 2, y: 2 }, { x: 8, y: 2 }] })
  client.load.mockResolvedValue({ reference: revision, draft })
  render(<MapAuthoring client={client} />)
  await user.click(screen.getByRole('button', { name: 'Load revision list' }))
  await user.click(await screen.findByRole('button', { name: 'Load revision-1' }))
  await user.click(await screen.findByRole('button', { name: 'Test route' }))
  expect(screen.getByLabelText('Hand-measured flight height · m')).toHaveValue(null)
  expect(screen.getByLabelText('Height measurement evidence')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Validate saved revision' })).toBeDisabled()
})
