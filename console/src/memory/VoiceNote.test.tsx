import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { VoiceNote } from './MemoryMedia'

const initialMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  if (initialMediaDevices) Object.defineProperty(navigator, 'mediaDevices', initialMediaDevices)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
})
class Recorder {
  static isTypeSupported = vi.fn((type: string) => type === 'audio/webm;codecs=opus')
  state = 'inactive'
  mimeType = 'audio/webm;codecs=opus'
  ondataavailable?: (event: { data: Blob }) => void
  onstop?: () => void
  onerror?: () => void
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['recording']) })
    this.onstop?.()
  }
}
function setup(getMedia?: ReturnType<typeof vi.fn>) {
  const stop = vi.fn()
  const source = { getTracks: () => [{ stop }] }
  const getUserMedia = getMedia ?? vi.fn().mockResolvedValue(source)
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
  vi.stubGlobal('MediaRecorder', Recorder)
  const onFile = vi.fn(),
    onActive = vi.fn()
  const view = render(<VoiceNote disabled={false} onFile={onFile} onActive={onActive} />)
  return { ...view, stop, source, onFile, onActive, getUserMedia }
}
async function start() {
  await act(async () =>
    fireEvent.click(screen.getByRole('button', { name: 'Record a voice memory' })),
  )
}

it('records only after a tap and stops the microphone at 60 seconds', async () => {
  vi.useFakeTimers()
  const { onFile, stop, getUserMedia, onActive } = setup()
  expect(getUserMedia).not.toHaveBeenCalled()
  await start()
  expect(getUserMedia).toHaveBeenCalledWith({ audio: true, video: false })
  expect(screen.getByRole('button', { name: /Stop recording/ })).toBeInTheDocument()
  await act(async () => vi.advanceTimersByTime(60000))
  expect(stop).toHaveBeenCalled()
  expect(onFile).toHaveBeenCalledOnce()
  expect(onFile.mock.calls[0][0].name).toBe('Voice memory.webm')
  expect(onActive).toHaveBeenLastCalledWith(false)
})

it('stops active microphone tracks when the editor unmounts without emitting a file', async () => {
  const { stop, onFile, unmount } = setup()
  await start()
  unmount()
  expect(stop).toHaveBeenCalled()
  expect(onFile).not.toHaveBeenCalled()
})

it('releases late permission results after closing the editor', async () => {
  let grant!: (value: unknown) => void
  const pending = vi.fn().mockReturnValue(
    new Promise((resolve) => {
      grant = resolve
    }),
  )
  const { source, stop, onFile, unmount } = setup(pending)
  await start()
  unmount()
  await act(async () => grant(source))
  expect(stop).toHaveBeenCalledOnce()
  expect(onFile).not.toHaveBeenCalled()
})

it('does not let an old permission request cancel a newer recording', async () => {
  let rejectOld!: (reason: unknown) => void
  const old = new Promise((_resolve, reject) => {
    rejectOld = reject
  })
  const stopNew = vi.fn()
  const pending = vi
    .fn()
    .mockReturnValueOnce(old)
    .mockResolvedValue({ getTracks: () => [{ stop: stopNew }] })
  setup(pending)
  await start()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel microphone request' }))
  await start()
  await act(async () => rejectOld(new Error('Old request denied')))
  expect(screen.getByRole('button', { name: /Stop recording/ })).toBeInTheDocument()
  expect(stopNew).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: /Stop recording/ }))
  expect(stopNew).toHaveBeenCalled()
})

it('stops recording when the page becomes hidden', async () => {
  const { stop, onFile } = setup()
  await start()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  fireEvent(document, new Event('visibilitychange'))
  expect(stop).toHaveBeenCalled()
  expect(onFile).toHaveBeenCalledOnce()
})

it('offers an upload fallback if the microphone is denied or unsupported', async () => {
  const { onFile, rerender } = setup(vi.fn().mockRejectedValue(new Error('Microphone denied')))
  await start()
  expect(screen.getByRole('alert')).toHaveTextContent('You can still choose an audio file')
  expect(onFile).not.toHaveBeenCalled()
  vi.stubGlobal('MediaRecorder', undefined)
  rerender(<VoiceNote disabled={false} onFile={vi.fn()} onActive={vi.fn()} />)
  expect(screen.getByText(/Voice recording isn’t available/)).toBeInTheDocument()
})
