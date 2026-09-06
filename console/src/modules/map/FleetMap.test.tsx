import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, test, vi } from 'vitest'
import App from '../../App'
import type { MapEndpoint } from '../../relay/map-endpoint'
import { callsNamed, canvasCalls } from '../../testing/canvas-context'
import { FixtureRelayClient } from '../../testing/fixture-relay-client'

const session = 'fleet-map-session'
const clock = () => 1_756_700_000_000

const endpoint: MapEndpoint = {
  url: 'https://relay.test/api/sessions/sweep-6/map',
  resetUrl: 'https://relay.test/api/sessions/sweep-6/map/reset',
  authorization: 'Bearer map-token',
}

const HEADERS = {
  'X-Sweep-Map-Resolution-M': '0.05',
  'X-Sweep-Map-Origin-X': '-5',
  'X-Sweep-Map-Origin-Y': '-4',
  'X-Sweep-Map-Width': '200',
  'X-Sweep-Map-Height': '160',
  'X-Sweep-Map-Updated-At': '1756700000000',
}

function mapResponse(): Response {
  return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }), {
    status: 200,
    headers: HEADERS,
  })
}

function renderConsole(mapEndpoint?: MapEndpoint) {
  return render(
    <App
      sessionId={session}
      clients={{
        console: new FixtureRelayClient(session, clock, 'console', 'mixed'),
        keyboard: new FixtureRelayClient(session, clock, 'keyboard', 'mixed'),
      }}
      intentDependencies={{ now: clock, nextId: () => 'map-intent' }}
      initialModule="reference"
      mapEndpoint={mapEndpoint}
    />,
  )
}

type User = ReturnType<typeof userEvent.setup>

async function openMap(user: User) {
  const tabs = within(screen.getByRole('group', { name: 'Reference sections' }))
  await user.click(tabs.getByRole('button', { name: 'Map' }))
  return screen.getByRole('img', { name: 'Fleet map' }) as HTMLCanvasElement
}

const reads = (fetcher: ReturnType<typeof vi.fn>) =>
  fetcher.mock.calls.filter((call) => call[0] === endpoint.url).length

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('Fleet map', () => {
  test('reads the occupancy map on mount, once a second while mounted, and not after unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const fetcher = vi.fn(async () => mapResponse())
    vi.stubGlobal('fetch', fetcher)
    vi.stubGlobal('createImageBitmap', async () => ({ width: 200, height: 160 }))
    const { unmount } = renderConsole(endpoint)
    const canvas = await openMap(user)

    await waitFor(() => expect(reads(fetcher)).toBe(1))
    expect(fetcher).toHaveBeenCalledWith(
      endpoint.url,
      expect.objectContaining({ headers: { Authorization: 'Bearer map-token' } }),
    )
    expect(await screen.findByText(/Occupancy map 200×160 cells at 0.05 m/)).toBeInTheDocument()
    await waitFor(() => expect(callsNamed(canvasCalls(canvas), 'drawImage')).not.toHaveLength(0))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(reads(fetcher)).toBe(2)

    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000)
    })
    expect(reads(fetcher)).toBe(2)
  })

  test('a relay with no grid yet says so and the fleet is still drawn', async () => {
    const user = userEvent.setup()
    const fetcher = vi.fn(async () => new Response(null, { status: 404 }))
    vi.stubGlobal('fetch', fetcher)
    renderConsole(endpoint)
    const canvas = await openMap(user)

    expect(
      await screen.findByText('The relay reports no occupancy map for this session yet.'),
    ).toBeInTheDocument()
    const labels = callsNamed(canvasCalls(canvas), 'fillText').map((args) => args[0])
    expect(new Set(labels)).toEqual(new Set(['D-01', 'D-02', 'G-01', 'G-02', 'G-03']))
    expect(callsNamed(canvasCalls(canvas), 'drawImage')).toHaveLength(0)
    // Two of the three ground vehicles carry the lidar kit in this scenario.
    const legend = within(screen.getByRole('list', { name: 'Scanning devices' }))
    expect(legend.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      expect.stringContaining('G-01 · 360 bins'),
      expect.stringContaining('G-02 · 360 bins'),
    ])
  })

  test('without a relay bootstrap nothing is read and the reset is unavailable', async () => {
    const user = userEvent.setup()
    const fetcher = vi.fn(async () => mapResponse())
    vi.stubGlobal('fetch', fetcher)
    renderConsole()
    await openMap(user)

    expect(
      screen.getByText(/No relay bootstrap, so no occupancy map can be read/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reset map' })).toBeDisabled()
    expect(fetcher).not.toHaveBeenCalled()
  })

  test('Reset map posts to the relay and reports what it answered', async () => {
    const user = userEvent.setup()
    const fetcher = vi.fn(async (url: string) =>
      url === endpoint.resetUrl ? new Response(null, { status: 204 }) : new Response(null, { status: 404 }),
    )
    vi.stubGlobal('fetch', fetcher)
    renderConsole(endpoint)
    await openMap(user)
    await screen.findByText('The relay reports no occupancy map for this session yet.')

    await user.click(screen.getByRole('button', { name: 'Reset map' }))
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(endpoint.resetUrl, {
        method: 'POST',
        cache: 'no-store',
        credentials: 'omit',
        headers: { Authorization: 'Bearer map-token' },
      }),
    )
    expect(await screen.findByText(/The relay cleared the occupancy grid/)).toBeInTheDocument()
  })

  test('panning moves the fleet under the pointer and zooming changes the scale', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 404 })))
    renderConsole(endpoint)
    const canvas = await openMap(user)
    expect(screen.getByText(/48 pixels per metre/)).toBeInTheDocument()

    const before = callsNamed(canvasCalls(canvas), 'fillText').at(-1)
    await act(async () => {
      canvas.dispatchEvent(
        new PointerEvent('pointerdown', { pointerId: 1, clientX: 100, clientY: 100, bubbles: true }),
      )
      canvas.dispatchEvent(
        new PointerEvent('pointermove', { pointerId: 1, clientX: 140, clientY: 130, bubbles: true }),
      )
    })
    const after = callsNamed(canvasCalls(canvas), 'fillText').at(-1)
    expect(after?.[1]).toBeCloseTo((before?.[1] as number) + 40, 6)
    expect(after?.[2]).toBeCloseTo((before?.[2] as number) + 30, 6)

    await user.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByText(/67 pixels per metre/)).toBeInTheDocument()
  })
})
