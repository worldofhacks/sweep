import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { CaptureComposer } from './CaptureComposer'
import { AtlasClient } from './client'

afterEach(() => vi.restoreAllMocks())

it('retains the original file and metadata when an upload must be retried', async () => {
  const client = new AtlasClient({
    baseUrl: 'https://example.test',
    sessionId: 'room',
    token: 'test-only',
  })
  const upload = vi
    .spyOn(client, 'upload')
    .mockRejectedValueOnce(new Error('Offline'))
    .mockResolvedValueOnce({ id: 'saved' } as never)
  const saved = vi.fn()
  const view = render(
    <CaptureComposer
      client={client}
      spaceId="space"
      contributor="person-one"
      name="Sam"
      onSaved={saved}
    />,
  )
  const file = new File(['image-content'], 'view.jpg', { type: 'image/jpeg', lastModified: 12345 })
  fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [file] } })
  await screen.findByRole('button', { name: 'Retry upload' })
  expect(saved).not.toHaveBeenCalled()
  const initial = upload.mock.calls[0]
  expect(initial[2]).toMatchObject({ source: 'import', position: null, captured_at: 12345 })
  await userEvent.click(screen.getByRole('button', { name: 'Retry upload' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  expect(upload.mock.calls[1][1]).toBe(file)
  expect(upload.mock.calls[1][2]).toBe(initial[2])
  expect(screen.queryByRole('button', { name: 'Retry upload' })).not.toBeInTheDocument()
})

it('stops a camera stream that opens after the capture dialog is closed', async () => {
  let resolve!: (value: MediaStream) => void
  const getUserMedia = vi.fn(
    () =>
      new Promise<MediaStream>((done) => {
        resolve = done
      }),
  )
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  const client = new AtlasClient({
    baseUrl: 'https://example.test',
    sessionId: 'room',
    token: 'test-only',
  })
  const view = render(
    <CaptureComposer
      client={client}
      spaceId="space"
      contributor="person-one"
      name="Sam"
      onSaved={() => {}}
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Enable camera' }))
  view.unmount()
  const stop = vi.fn()
  resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream)
  await waitFor(() => expect(stop).toHaveBeenCalledOnce())
  vi.unstubAllGlobals()
})
