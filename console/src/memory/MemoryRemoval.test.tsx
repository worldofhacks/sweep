import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AtlasClient } from '../atlas/client'
import { NativeAtlasClient } from '../atlas/native'
import MemoryRemoval from './MemoryRemoval'
import SpaceRemovals from './SpaceRemovals'
import { readRemoval, type RemovalPreview, type RemovalReceipt } from './removal'

const preview: RemovalPreview = { capture_id: 'photo', state: 'preview', confirmation: 'a'.repeat(64), recordings: 2, builds: 1, analysis_pending: false }
const receipt: RemovalReceipt = { capture_id: 'photo', state: 'cleanup_pending', requested_at: 1788950000000, requested_by: 'workspace-operator', completed_at: null, recordings: 2, builds: 1, analysis_pending: false }
const showModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
beforeEach(() => Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', '') } }))
afterEach(() => {
  vi.restoreAllMocks(); vi.useRealTimers()
  if (showModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', showModal)
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
})
function setup() {
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'test', token: 'unit-test' })
  const read = vi.spyOn(client, 'removal').mockResolvedValue(preview)
  const remove = vi.spyOn(client, 'removeCapture').mockResolvedValue(receipt)
  const props = { client, spaceId: 'garden', captureId: 'photo', onRemoved: vi.fn(), onCancel: vi.fn(), onBusy: vi.fn() }
  return { ...props, props, read, remove }
}
async function confirm() {
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Remove from this Space' }))
}
it('previews without mutation and requires explicit confirmation of dependent builds', async () => {
  const test = setup()
  render(<MemoryRemoval {...test.props} />)
  expect(await screen.findByText('1 shared 3D build will also be withdrawn.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Remove from this Space' })).toBeDisabled()
  expect(test.remove).not.toHaveBeenCalled()
  expect(screen.getByText(/Backups, files people downloaded/)).toBeInTheDocument()
  await confirm()
  await waitFor(() => expect(test.onRemoved).toHaveBeenCalledOnce())
  expect(test.remove).toHaveBeenCalledExactlyOnceWith('garden', 'photo', preview.confirmation)
})
it('resolves an ambiguous accepted response using a read, never a second POST', async () => {
  const test = setup()
  test.remove.mockRejectedValue(new Error('connection lost'))
  test.read.mockResolvedValueOnce(preview).mockResolvedValue(receipt)
  render(<MemoryRemoval {...test.props} />)
  await confirm()
  await waitFor(() => expect(test.onRemoved).toHaveBeenCalledOnce())
  expect(test.read).toHaveBeenCalledTimes(2)
  expect(test.remove).toHaveBeenCalledOnce()
})
it('requires a fresh review and checkbox after a conflict', async () => {
  const test = setup()
  test.remove.mockRejectedValue(new Error('revision conflict'))
  test.read.mockResolvedValueOnce(preview).mockResolvedValue({ ...preview, builds: 3, confirmation: 'b'.repeat(64) })
  render(<MemoryRemoval {...test.props} />)
  await confirm()
  expect(await screen.findByRole('alert')).toHaveTextContent('Review the current impact')
  expect(screen.getByText('3 shared 3D builds will also be withdrawn.')).toBeInTheDocument()
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(screen.getByRole('button', { name: 'Remove from this Space' })).toBeDisabled()
  expect(test.onRemoved).not.toHaveBeenCalled()
})
it('blocks repeat deletion when both the response and its status are unknown', async () => {
  const test = setup()
  test.remove.mockRejectedValue(new Error('timeout'))
  test.read.mockResolvedValueOnce(preview).mockRejectedValue(new Error('offline'))
  render(<MemoryRemoval {...test.props} />)
  await confirm()
  expect(await screen.findByRole('alert')).toHaveTextContent('could not verify')
  expect(screen.queryByRole('button', { name: 'Remove from this Space' })).not.toBeInTheDocument()
  test.read.mockResolvedValue(receipt)
  fireEvent.click(screen.getByRole('button', { name: 'Check removal status' }))
  await waitFor(() => expect(test.onRemoved).toHaveBeenCalledOnce())
  expect(test.remove).toHaveBeenCalledOnce()
})
it('ignores a late preview after scope unmount and never starts deletion on load', async () => {
  const test = setup()
  let resolve!: (value: RemovalReceipt) => void
  test.read.mockReturnValue(new Promise(done => { resolve = done }))
  const view = render(<MemoryRemoval {...test.props} />)
  view.unmount()
  await act(async () => resolve(receipt))
  expect(test.read.mock.calls[0][2]?.aborted).toBe(true)
  expect(test.onRemoved).not.toHaveBeenCalled()
  expect(test.remove).not.toHaveBeenCalled()
})
it('keeps pending totals visible across pages and clears receipts after access refusal', async () => {
  const test = setup()
  const read = vi.spyOn(test.client, 'removals').mockResolvedValue({ receipts: [receipt], pending: 1, completed: 20, scope: 'own', next_before: 4 })
  render(<SpaceRemovals client={test.client} spaceId="garden" onClose={vi.fn()} />)
  expect(await screen.findByText(/1 cleanup pending · 20 local/)).toHaveTextContent('Your contributions only')
  expect(screen.getByText(/timeout is not proof/)).toBeInTheDocument()
  read.mockResolvedValue({ receipts: [{ ...receipt, capture_id: 'older', state: 'local_removed', completed_at: 1788950000100 }], pending: 1, completed: 20, scope: 'own', next_before: null })
  fireEvent.click(screen.getByRole('button', { name: 'Older receipts' }))
  expect(await screen.findByText('Capture older')).toBeInTheDocument()
  expect(read).toHaveBeenLastCalledWith('garden', expect.any(AbortSignal), 4)
  read.mockRejectedValue(new Error('Access removed'))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Access removed')
  expect(screen.queryByText('Capture older')).not.toBeInTheDocument()
  expect(test.remove).not.toHaveBeenCalled()
})
it('checks pending receipts without offering force completion', async () => {
  vi.useFakeTimers()
  const test = setup()
  const read = vi.spyOn(test.client, 'removals').mockResolvedValue({ receipts: [receipt], pending: 1, completed: 0, scope: 'space', next_before: null })
  const view = render(<SpaceRemovals client={test.client} spaceId="garden" onClose={vi.fn()} />)
  await act(async () => {})
  read.mockResolvedValue({ receipts: [{ ...receipt, state: 'local_removed', completed_at: 1788950000100 }], pending: 0, completed: 1, scope: 'space', next_before: null })
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
  expect(screen.getByText('Local cleanup complete')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
  expect(read).toHaveBeenCalledTimes(4)
  view.unmount()
  expect(test.remove).not.toHaveBeenCalled()
})
it('rejects malformed, mismatched, or unrecognized removal status', () => {
  expect(() => readRemoval({ ...receipt, state: 'erased_everywhere' })).toThrow()
  expect(() => readRemoval({ ...preview, confirmation: 'not-a-confirmation' })).toThrow()
  expect(() => readRemoval(receipt, 'another-photo')).toThrow()
  expect(() => readRemoval({ ...receipt, builds: -1 })).toThrow()
  expect(() => readRemoval({ ...receipt, state: 'local_removed', completed_at: null })).toThrow()
  expect(() => readRemoval({ ...receipt, state: 'local_removed', completed_at: 1 })).toThrow()
})

it('uses scoped encoded routes and never admits native removal writes', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ ...receipt, capture_id: 'photo/one' }), { headers: { 'Content-Type': 'application/json' } })))
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'test', token: 'unit-test' }, fetcher)
  await client.removeCapture('garden/one', 'photo/one', preview.confirmation)
  expect(fetcher).toHaveBeenCalledWith('https://example.test/api/sessions/test/atlas/spaces/garden%2Fone/captures/photo%2Fone/removal', expect.objectContaining({ method: 'POST', cache: 'no-store', body: JSON.stringify({ confirmation: preview.confirmation }) }))
  const native = new NativeAtlasClient({ id: 'native', baseUrl: 'https://example.test', sessionId: 'test', space: null })
  expect(native.memoryRemovalSupported).toBe(false)
  await expect(native.removeCapture('garden', 'photo', preview.confirmation)).rejects.toThrow('web console')
})

it('validates bounded receipt pages and monotonic cursors', async () => {
  const test = setup()
  const request = vi.spyOn(test.client.http, 'request').mockResolvedValue({ receipts: [receipt], scope: 'own', pending: 1, completed: 0, next_before: 5 })
  await expect(test.client.removals('garden', undefined, 4)).rejects.toThrow('could not be verified')
  request.mockResolvedValue({ receipts: Array(21).fill(receipt), scope: 'own', pending: 1, completed: 0, next_before: null })
  await expect(test.client.removals('garden')).rejects.toThrow('could not be verified')
  await expect(test.client.removals('garden', undefined, -1)).rejects.toThrow('existing receipt page')
})

it('restores focus to the receipt entry point when the dialog closes', async () => {
  const test = setup()
  vi.spyOn(test.client, 'removals').mockResolvedValue({ receipts: [], pending: 0, completed: 0, scope: 'own', next_before: null })
  const trigger = document.createElement('button')
  trigger.textContent = 'Receipt entry'
  document.body.append(trigger)
  trigger.focus()
  const view = render(<SpaceRemovals client={test.client} spaceId="garden" onClose={vi.fn()} />)
  await screen.findByText('No removal receipts on this page.')
  screen.getByRole('button', { name: 'Close dialog' }).focus()
  view.unmount()
  expect(document.activeElement).toBe(trigger)
  trigger.remove()
})
