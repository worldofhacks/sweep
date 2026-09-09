import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SpaceTimeline from './SpaceTimeline'
import { AtlasClient } from './client'
import { chapter, dateLabel, utcLabel, type TimelineEntry } from './timeline'

vi.mock('./SpacesModule', () => ({ MediaCard: ({ capture, timeCaption }: { capture: { id: string }; timeCaption: string }) => <div>Original {capture.id}<span>{timeCaption}</span></div> }))
const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
beforeEach(() => Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', '') } }))
afterEach(() => {
  vi.restoreAllMocks(); vi.useRealTimers()
  if (originalShowModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal)
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
})
function entry(id: string, date: string | null = '2020-06-01'): TimelineEntry {
  return { capture: { id, contributor_id: 'acct_alex', name: 'Alex', kind: 'photo', source: 'import', captured_at: null, uploaded_at: 1788946003000, position: null, note: '', mime: 'image/png', bytes: 10, sha256: 'a'.repeat(64) },
    time: { precision: date ? 'day' : 'unknown', value: date, end: null, note: '', source: date ? 'declared' : 'unknown', start_date: date, end_date: date },
    revision: 1, evidence: [], warnings: [], can_edit: true }
}
function setup(entries: TimelineEntry[]) {
  const client = new AtlasClient({ baseUrl: 'https://atlas.example', sessionId: 'test', token: 'local-test' })
  const read = vi.spyOn(client, 'timeline').mockResolvedValue({ entries, total: entries.length, ordering: 'calendar' })
  vi.spyOn(client, 'dateHistory').mockResolvedValue([])
  const correct = vi.spyOn(client, 'correctDate').mockRejectedValue(new Error('This date changed in another window. Reload before correcting it.'))
  const onVisible = vi.fn()
  return { client, read, correct, onVisible }
}
it('keeps historical chapters separate from upload time and maps only the displayed page', async () => {
  const entries = [...Array.from({ length: 25 }, (_, index) => entry(`old-${index}`)), entry('unknown', null)]
  const value = setup(entries)
  render(<SpaceTimeline {...value} spaceId="garden" />)
  expect(await screen.findByText('26 memories · 25 with dates', { exact: false })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'June 2020' })).toBeInTheDocument()
  expect(screen.queryByText('Original old-24')).not.toBeInTheDocument()
  await waitFor(() => expect(value.onVisible).toHaveBeenLastCalledWith(entries.slice(0, 24).map(e => e.capture.id)))
  fireEvent.click(screen.getByRole('button', { name: 'Show 2 more memories' }))
  expect(screen.getByText('Original old-24')).toBeInTheDocument()
  expect(screen.getByText('Date unknown')).toBeInTheDocument()
  fireEvent.click(screen.getByText(/Jump to a chapter/))
  fireEvent.click(screen.getByRole('button', { name: 'Still finding its date' }))
  expect(screen.queryByText('Original old-0')).not.toBeInTheDocument()
  expect(value.onVisible).toHaveBeenLastCalledWith(['unknown'])
  expect(screen.getByText(/1 has no recorded location/)).toBeInTheDocument()
})

it('retains edits after a conflict and sends a separate revisioned correction', async () => {
  const value = setup([entry('photo')])
  render(<SpaceTimeline {...value} spaceId="garden" />)
  fireEvent.click(await screen.findByRole('button', { name: 'Date details & correction' }))
  fireEvent.change(screen.getByLabelText('Memory date'), { target: { value: '2021-06-01' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save date correction' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('changed in another window')
  expect(screen.getByLabelText('Memory date')).toHaveValue('2021-06-01')
  expect(value.correct).toHaveBeenCalledWith('garden', 'photo', 1, { precision: 'day', value: '2021-06-01', end: null, note: '' })
})

it('does not lose an open correction when another window moves its chapter', async () => {
  vi.useFakeTimers()
  const value = setup([entry('photo')])
  await act(async () => { render(<SpaceTimeline {...value} spaceId="garden" />) })
  fireEvent.click(screen.getByRole('button', { name: 'Date details & correction' }))
  fireEvent.change(screen.getByLabelText('Memory date'), { target: { value: '2021-01-01' } })
  value.read.mockResolvedValue({ entries: [{ ...entry('photo', '2022-01-01'), revision: 2 }], total: 1, ordering: 'calendar' })
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
  expect(screen.getByLabelText('Memory date')).toHaveValue('2021-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Save date correction' }))
  expect(value.correct.mock.calls[0][2]).toBe(1)
})

it('removes stale private entries and map markers on a refused refresh', async () => {
  vi.useFakeTimers()
  const value = setup([{ ...entry('private'), can_edit: false }])
  await act(async () => { render(<SpaceTimeline {...value} spaceId="garden" />) })
  expect(screen.queryByRole('button', { name: 'Date details & correction' })).not.toBeInTheDocument()
  value.read.mockRejectedValue(new Error('Membership removed'))
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
  expect(screen.queryByText('Original private')).not.toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('Membership removed')
  expect(value.onVisible).toHaveBeenLastCalledWith([])
})

it('renders an honest empty state and never starts a correction merely by loading', async () => {
  const value = setup([])
  render(<SpaceTimeline {...value} spaceId="garden" />)
  expect(await screen.findByText('Your first chapter is waiting.')).toBeInTheDocument()
  await waitFor(() => expect(value.onVisible).toHaveBeenLastCalledWith([]))
  expect(value.correct).not.toHaveBeenCalled()
})

it('labels approximate dates and offset-bearing times without browser-zone conversion', () => {
  expect(dateLabel({ precision: 'year', value: '2020', end: null, note: '' })).toBe('2020 · year only')
  expect(dateLabel({ precision: 'instant', value: '2024-11-03T01:30:00-06:00', end: null, note: '' })).toContain('01:30:00 UTC-06:00')
  const range = entry('range')
  range.time = { ...range.time, precision: 'range', value: '2020-12-28', end: '2021-01-04', start_date: '2020-12-28', end_date: '2021-01-04' }
  expect(chapter(range).key).toBe('range:2020-12-28:2021-01-04')
  expect(utcLabel(Number.NaN)).toBe('Unrecognized source timestamp')
})
