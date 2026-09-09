import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { DetectionFeed } from './DetectionFeed'
import { HttpLiveDetectionClient, type DetectionFrame } from '../../media/detections'
import { PlatformHttp } from '../../platform/http'

const source = { drone_id: 11, connection_epoch: 2, camera_id: 'front', stream: 'ground-1-front' }
const frame: DetectionFrame = { sequence: 1, width: 64, height: 32, age_ms: 50,
  jpeg_base64: '/9j/AAAA', detections: [{ label: 'person', confidence: .9, bbox_xyxy: [2, 3, 40, 30] }] }
afterEach(() => vi.useRealTimers())

test('camera-bound HTTP client preserves deployment base path and rejects mismatched evidence', async () => {
  const payload = { session: 'test', device_id: 11, camera_id: 'front', connection_epoch: 2,
    stream: 'ground-1-front', state: 'live', frame }
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(payload), { status: 200 }))
  const client = new HttpLiveDetectionClient(new PlatformHttp({ baseUrl: 'wss://relay.example/field/', token: 'isolated', sessionId: 'test' }, fetcher))
  expect((await client.snapshot(source)).frame).toEqual(frame)
  expect(fetcher.mock.calls[0]?.[0]).toBe('https://relay.example/field/api/sessions/test/live-detections/11/front/2')
  payload.connection_epoch = 3
  await expect(client.snapshot(source)).rejects.toThrow('mismatched camera')
  payload.connection_epoch = 2
  payload.frame = { ...frame, detections: [{ ...frame.detections[0], bbox_xyxy: [0, 0, 400, 300] }] }
  await expect(client.snapshot(source)).rejects.toThrow('mismatched camera')
})

test('draws boxes with their analyzed image and retires them when requests stop arriving', async () => {
  vi.useFakeTimers()
  const snapshot = vi.fn().mockResolvedValueOnce({ state: 'live', frame }).mockImplementation(() => new Promise(() => {}))
  render(<DetectionFeed client={{ snapshot }} source={source} />)
  await act(async () => {})
  const svg = screen.getByRole('img', { name: 'Live object detection overlay' })
  expect(svg).toHaveAttribute('viewBox', '0 0 64 32')
  expect(svg.querySelector('image')).toHaveAttribute('href', 'data:image/jpeg;base64,/9j/AAAA')
  expect(svg.querySelector('rect')).toHaveAttribute('x', '2')
  expect(screen.getByText('person 90%')).toBeInTheDocument()
  await act(async () => { vi.advanceTimersByTime(1500) })
  expect(screen.queryByRole('img', { name: 'Live object detection overlay' })).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('stale')
})

test('camera switch aborts old read and clears its detections', async () => {
  const snapshot = vi.fn().mockResolvedValueOnce({ state: 'live', frame }).mockResolvedValue({ state: 'unconfigured', frame: null })
  const client = { snapshot }
  const view = render(<DetectionFeed client={client} source={source} />)
  await screen.findByText('person 90%')
  const signal = snapshot.mock.calls[0][1] as AbortSignal
  view.rerender(<DetectionFeed client={client} source={{ ...source, camera_id: 'rear', stream: 'ground-1-rear' }} />)
  await waitFor(() => expect(screen.queryByText('person 90%')).not.toBeInTheDocument())
  expect(signal.aborted).toBe(true)
  expect(await screen.findByText('Object detection is not configured for this camera.')).toBeInTheDocument()
})
