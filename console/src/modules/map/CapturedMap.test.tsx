import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { CapturedMap } from './CapturedMap'

const preview = {
  version: 1,
  title: 'Office capture',
  description: 'Captured tags and provisional wall segments.',
  images: [{ file: 'tags.png', caption: 'Tag layout' }, { file: 'walls.png', caption: 'Wall overlay' }],
}

afterEach(() => vi.unstubAllGlobals())

test('displays installed capture, changes layer and zoom, and offers no approval or motion action', async () => {
  const request = vi.fn().mockResolvedValue({ ok: true, json: async () => preview })
  vi.stubGlobal('fetch', request)
  render(<CapturedMap />)
  expect(await screen.findByRole('heading', { name: 'Office capture' })).toBeInTheDocument()
  expect(request).toHaveBeenCalledWith('/mapping-preview/manifest.json', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }))
  expect(screen.getByText('Provisional · awaiting calibration and map approval')).toBeInTheDocument()
  expect(screen.getByRole('img', { name: 'Tag layout' })).toHaveAttribute('src', '/mapping-preview/tags.png')
  fireEvent.click(screen.getByRole('button', { name: 'Wall overlay' }))
  expect(screen.getByRole('img', { name: 'Wall overlay' })).toHaveAttribute('src', '/mapping-preview/walls.png')
  fireEvent.change(screen.getByRole('slider', { name: /Zoom/ }), { target: { value: '200' } })
  expect(screen.getByRole('img')).toHaveStyle({ width: '200%' })
  expect(screen.getByRole('link', { name: 'Open full image' })).toHaveAttribute('href', '/mapping-preview/walls.png')
  expect(screen.queryByRole('button', { name: /approve|activate|navigate|fly/i })).not.toBeInTheDocument()
  expect(request).toHaveBeenCalledTimes(1)
})

test.each(['../private.png', 'https://elsewhere.test/map.png', 'data:image/png;base64,abc', 'tags.svg'])('refuses image paths outside the installed PNG directory: %s', async (file) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ...preview, images: [{ file, caption: 'Invalid' }] }) }))
  render(<CapturedMap />)
  expect(await screen.findByText('No captured map is installed on this console.')).toBeInTheDocument()
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
})

test('reports absent capture without substituting an example map', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  render(<CapturedMap />)
  expect(await screen.findByText('No captured map is installed on this console.')).toBeInTheDocument()
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
})

test('shows an image load failure and allows another layer to load', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => preview }))
  render(<CapturedMap />)
  fireEvent.error(await screen.findByRole('img'))
  expect(screen.getByText('This captured map image could not be loaded.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Wall overlay' }))
  expect(screen.getByRole('img', { name: 'Wall overlay' })).toBeInTheDocument()
})

test('cancels a pending manifest request when leaving the captured view', async () => {
  const request = vi.fn().mockImplementation(() => new Promise(() => {}))
  vi.stubGlobal('fetch', request)
  const view = render(<CapturedMap />)
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
  const options = request.mock.calls[0][1] as RequestInit
  view.unmount()
  expect(options.signal?.aborted).toBe(true)
})
