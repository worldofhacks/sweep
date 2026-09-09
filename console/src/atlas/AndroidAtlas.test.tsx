import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { AndroidAtlas } from './AndroidAtlas'
import type { NativeCaptureRequest } from './SpacesModule'
import type { NativeAtlasClient, NativeSession, NativeUpload } from './native'

vi.mock('./SpacesModule', async importOriginal => {
  const original = await importOriginal<typeof import('./SpacesModule')>()
  return { ...original, SpacesModule: ({ captureNative, services }: { captureNative: (request: NativeCaptureRequest) => Promise<void>; services: { atlas?: NativeAtlasClient } }) =>
    <><button onClick={() => void captureNative({ spaceId: 'austin-space', title: 'Shoal Creek · Austin', contributor: 'person-one', name: 'Sam' })}>Add a capture</button>
      <button onClick={() => void captureNative({ spaceId: 'austin-space', title: 'Shoal Creek · Austin', contributor: 'person-one', name: 'Sam', request: requestedView })}>Contribute requested view</button>
      <button onClick={() => void services.atlas?.detail('austin-space')}>Read space</button></> }
})
const requestedView = { target: { kind: 'location' as const, cell_id: '5:5' }, label: 'Creek viewpoint', note: 'Photograph from the public path.' }
const session: NativeSession = { id: 'credential-one', baseUrl: 'https://relay.example', sessionId: 'workspace-one', space: 'austin-space' }
// jsdom does not implement the native modal API; real dialog layout/focus is
// exercised separately with the built Android assets in Chromium.
const modalDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal')
beforeAll(() => Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
  configurable: true, value: function (this: HTMLDialogElement) { this.open = true },
}))
afterAll(() => {
  if (modalDescriptor) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', modalDescriptor)
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
})
function bridge(uploads: NativeUpload[] = [], action: (op: string) => unknown = () => true) {
  const api = { onmessage: null as ((event: { data: string }) => void) | null, postMessage: vi.fn((raw: string) => {
    const request = JSON.parse(raw)
    queueMicrotask(() => {
      try {
        const result = request.op === 'getSession' ? session : request.op === 'getUploads' ? uploads : action(request.op)
        api.onmessage?.({ data: JSON.stringify({ id: request.id, result }) })
      } catch (error) { api.onmessage?.({ data: JSON.stringify({ id: request.id, error: (error as Error).message, code: (error as { code?: string }).code }) }) }
    })
  }) }
  window.SweepAtlasNative = api
  return { calls: () => api.postMessage.mock.calls.map(([raw]) => JSON.parse(raw)) }
}
afterEach(() => { delete window.SweepAtlasNative; vi.restoreAllMocks() })

it.each(['capture', 'importMedia'] as const)('carries requested view context into native %s', async op => {
  const api = bridge()
  render(<AndroidAtlas />)
  fireEvent.click(await screen.findByRole('button', { name: 'Contribute requested view' }))
  expect(screen.getByLabelText('Requested view')).toHaveTextContent(requestedView.note)
  fireEvent.click(screen.getByRole('button', { name: op === 'capture' ? /Use your camera/ : /Import from device/ }))
  await waitFor(() => expect(api.calls().find(call => call.op === op)).toMatchObject({ payload: { request: requestedView, session: session.id, spaceId: 'austin-space' } }))
})

it.each(['capture', 'importMedia'] as const)('opens only the selected %s capability, bound to the source workspace and space', async op => {
  const api = bridge()
  const user = userEvent.setup()
  render(<AndroidAtlas />)
  await user.click(await screen.findByRole('button', { name: 'Add a capture' }))
  const dialog = screen.getByRole('dialog', { name: 'Add your perspective' })
  expect(dialog).toHaveTextContent('Shoal Creek · Austin')
  expect(api.calls().map(value => value.op)).toEqual(['getSession', 'getUploads'])
  await user.click(within(dialog).getByRole('button', { name: op === 'capture' ? /Use your camera/ : /Import from device/ }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(api.calls().filter(value => ['capture', 'importMedia'].includes(value.op))).toEqual([
    expect.objectContaining({ op, payload: { session: 'credential-one', spaceId: 'austin-space', title: 'Shoal Creek · Austin', contributor: 'person-one', name: 'Sam' } }),
  ])
  if (op === 'importMedia') expect(screen.getByRole('heading', { name: 'Your perspectives.' })).toBeInTheDocument()
})

it('backs out of source selection without invoking a camera, picker, or app exit', async () => {
  const api = bridge()
  render(<AndroidAtlas />)
  fireEvent.click(await screen.findByRole('button', { name: 'Add a capture' }))
  act(() => window.dispatchEvent(new Event('atlas-back')))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(api.calls().map(value => value.op)).toEqual(['getSession', 'getUploads'])
})

it('keeps a refused picker error visible inside the source dialog and permits retry', async () => {
  bridge([], () => { throw new Error('The original workspace is unavailable.') })
  const user = userEvent.setup()
  render(<AndroidAtlas />)
  await user.click(await screen.findByRole('button', { name: 'Add a capture' }))
  await user.click(screen.getByRole('button', { name: /Import from device/ }))
  const dialog = screen.getByRole('dialog')
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('The original workspace is unavailable.')
  expect(within(dialog).getByRole('button', { name: /Import from device/ })).toBeEnabled()
})

it('distinguishes an incomplete import from queued originals and verified uploads', async () => {
  const base: NativeUpload = { id: 'import-one', spaceId: 'austin-space', kind: 'photo', source: 'import', finalized: false,
    state: 'importing', bytes: 0, sent: 4096, error: '', createdAt: 1788900000000, displayName: 'Austin creek – original.png' }
  bridge([base, { ...base, id: 'import-two', state: 'failed', error: 'File moved.' },
    { ...base, id: 'import-three', state: 'queued', finalized: true, bytes: 2000 },
    { ...base, id: 'import-four', state: 'saved', finalized: true, bytes: 2000 }])
  render(<AndroidAtlas />)
  await screen.findByRole('button', { name: 'Add a capture' })
  fireEvent.click(screen.getByRole('button', { name: /Uploads/ }))
  const copying = screen.getByText('Copying from device').closest('article')!
  expect(within(copying).getByRole('heading')).toHaveTextContent('Austin creek – original.png')
  expect(within(copying).getByRole('progressbar')).not.toHaveAttribute('value')
  expect(within(copying).queryByRole('button')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry import' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry upload' })).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Export original' })).toHaveLength(2)
  expect(screen.getByRole('button', { name: 'Export local file' })).toBeInTheDocument()
  expect(screen.getAllByText(/Capture time and location unknown/)).toHaveLength(4)
})

it.each(['queued', 'saved'] as const)('keeps cached-space status separate from %s upload status', async state => {
  let connected = false
  const detail = { space: { id: 'austin-space' }, people: [] }
  const upload: NativeUpload = { id: 'capture-one', spaceId: 'austin-space', kind: 'photo', source: 'camera',
    finalized: true, state, bytes: 2048, sent: 0, error: '', createdAt: 1788900000000 }
  bridge([upload], op => {
    if (op === 'cachedSpaces') return [detail]
    if (op === 'request') {
      if (!connected) throw Object.assign(new Error('No connection'), { code: 'network' })
      return { status: 200, body: JSON.stringify(detail) }
    }
    return true
  })
  render(<AndroidAtlas />)
  fireEvent.click(await screen.findByRole('button', { name: 'Read space' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Saved spaces, no live locations')
  fireEvent.click(screen.getByRole('button', { name: /Uploads/ }))
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: /^Photo/ })).toHaveTextContent(state === 'saved' ? 'Saved ✓' : 'Waiting to upload')
  fireEvent.click(screen.getByRole('button', { name: 'Spaces' }))
  expect(screen.getByRole('status')).toHaveTextContent('Saved spaces, no live locations')
  connected = true
  fireEvent.click(screen.getByRole('button', { name: 'Read space' }))
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
})
