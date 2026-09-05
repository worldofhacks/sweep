import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { WhepVideo } from './WhepVideo'

class Peer extends EventTarget {
  static instances: Peer[] = []
  iceGatheringState = 'complete'
  connectionState = 'new'
  localDescription = { type: 'offer', sdp: 'actual-offer-sdp' }
  ontrack: ((event: { streams: unknown[] }) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  addTransceiver = vi.fn()
  createOffer = vi.fn(async () => this.localDescription)
  setLocalDescription = vi.fn(async () => undefined)
  setRemoteDescription = vi.fn(async () => undefined)
  close = vi.fn()
  stopTrack = vi.fn()
  getReceivers = () => [{ track: { stop: this.stopTrack } }]
  constructor() { super(); Peer.instances.push(this) }
}
const request = vi.fn<typeof fetch>()
beforeEach(() => {
  Peer.instances = []
  request.mockReset()
  request.mockImplementation(async (_url, init) => init?.method === 'DELETE'
    ? new Response(null, { status: 204 })
    : new Response('answer-sdp', { status: 201, headers: { Location: '/drone1/whep/session-1' } }))
  vi.stubGlobal('RTCPeerConnection', Peer)
  vi.stubGlobal('fetch', request)
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

test('negotiates video, reports live only on playing, and deletes session on teardown', async () => {
  const view = render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  expect(screen.getByText('Connecting to camera…')).toBeInTheDocument()
  await waitFor(() => expect(Peer.instances[0].setRemoteDescription).toHaveBeenCalledWith({ type: 'answer', sdp: 'answer-sdp' }))
  expect(request).toHaveBeenCalledWith('http://localhost:8889/drone1/whep', expect.objectContaining({
    method: 'POST', body: 'actual-offer-sdp', credentials: 'omit',
  }))
  const peer = Peer.instances[0]
  const stream = { id: 'real-inbound-stream' }
  act(() => peer.ontrack?.({ streams: [stream] }))
  const video = screen.getByLabelText('Live camera drone1') as HTMLVideoElement
  expect(video.srcObject).toBe(stream)
  expect(screen.queryByText('Live video')).not.toBeInTheDocument()
  fireEvent.playing(video)
  expect(screen.getByText('Live video')).toBeInTheDocument()
  view.unmount()
  expect(peer.close).toHaveBeenCalledOnce()
  expect(peer.stopTrack).toHaveBeenCalledOnce()
  expect(video.srcObject).toBeNull()
  expect(request).toHaveBeenCalledWith('http://localhost:8889/drone1/whep/session-1', expect.objectContaining({ method: 'DELETE' }))
})

test('switching focus releases the previous peer and connects the new path', async () => {
  const view = render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  await waitFor(() => expect(Peer.instances[0].setRemoteDescription).toHaveBeenCalled())
  const previous = Peer.instances[0]
  view.rerender(<WhepVideo droneId={2} baseUrl="http://localhost:8889" />)
  await waitFor(() => expect(request).toHaveBeenCalledWith('http://localhost:8889/drone2/whep', expect.objectContaining({ method: 'POST' })))
  expect(previous.close).toHaveBeenCalledOnce()
  expect(screen.getByLabelText('Live camera drone2')).toBeInTheDocument()
})

test('HTTP failure is visible and retry makes a new connection', async () => {
  request.mockResolvedValueOnce(new Response(null, { status: 404 }))
  render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Camera connection lost or unavailable.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry camera' }))
  await waitFor(() => expect(Peer.instances).toHaveLength(2))
  await waitFor(() => expect(Peer.instances[1].setRemoteDescription).toHaveBeenCalled())
  expect(screen.getByText('Connecting to camera…')).toBeInTheDocument()
})

test('peer disconnect clears stale video and reports failure', async () => {
  render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  await waitFor(() => expect(Peer.instances[0].setRemoteDescription).toHaveBeenCalled())
  const peer = Peer.instances[0]
  act(() => { peer.connectionState = 'disconnected'; peer.onconnectionstatechange?.() })
  expect(screen.getByRole('alert')).toBeInTheDocument()
  expect(peer.close).toHaveBeenCalledOnce()
})

test('a connection without rendered video times out', async () => {
  vi.useFakeTimers()
  render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(screen.getByRole('alert')).toBeInTheDocument()
  expect(Peer.instances[0].close).toHaveBeenCalledOnce()
})

test.each(['', 'ftp://localhost', 'https://user:secret@host', 'https://host?token=secret'])('does not connect with invalid or credential-bearing configuration %s', (baseUrl) => {
  render(<WhepVideo droneId={1} baseUrl={baseUrl} />)
  expect(screen.getByText('Camera playback is not configured.')).toBeInTheDocument()
  expect(request).not.toHaveBeenCalled()
  expect(Peer.instances).toHaveLength(0)
})


test('late POST response after unmount releases its server resource without attaching video', async () => {
  let finish: (response: Response) => void = () => { throw new Error('POST not started') }
  request.mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve }))
  const view = render(<WhepVideo droneId={1} baseUrl="http://localhost:8889" />)
  await waitFor(() => expect(request).toHaveBeenCalledOnce())
  const peer = Peer.instances[0]
  view.unmount()
  await act(async () => {
    finish(new Response('late-answer', { status: 201, headers: { Location: '/drone1/whep/late' } }))
  })
  expect(peer.close).toHaveBeenCalledOnce()
  expect(peer.setRemoteDescription).not.toHaveBeenCalled()
  expect(request).toHaveBeenCalledWith('http://localhost:8889/drone1/whep/late', expect.objectContaining({ method: 'DELETE' }))
})
