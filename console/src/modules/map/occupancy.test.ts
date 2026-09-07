import { describe, expect, test, vi } from 'vitest'
import type { MapEndpoint } from '../../relay/map-endpoint'
import { fetchOccupancyMap, resetOccupancyMap } from './occupancy'

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

function pngResponse(headers: Record<string, string> = HEADERS, status = 200): Response {
  return new Response(new Uint8Array([137, 80, 78, 71]), {
    status,
    headers,
  })
}

const decode = vi.fn(async () => ({ width: 200, height: 160 }) as unknown as CanvasImageSource)

describe('occupancy map endpoint', () => {
  test('reads the PNG and its geometry with the relay bearer', async () => {
    const fetcher = vi.fn(async () => pngResponse())
    const result = await fetchOccupancyMap(endpoint, { fetcher, decode })
    expect(fetcher).toHaveBeenCalledWith(endpoint.url, {
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: 'Bearer map-token' },
      signal: undefined,
    })
    expect(result).toEqual({
      status: 'map',
      map: {
        image: { width: 200, height: 160 },
        resolution_m: 0.05,
        origin_x: -5,
        origin_y: -4,
        width: 200,
        height: 160,
        updated_at: 1_756_700_000_000,
      },
    })
  })

  test('a relay with no grid for the session is absent, not an error', async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 404 }))
    expect(await fetchOccupancyMap(endpoint, { fetcher, decode })).toEqual({ status: 'absent' })
  })

  test('a refusal, a header that does not describe a grid, or an undecodable body is an error', async () => {
    const refused = vi.fn(async () => new Response(null, { status: 401 }))
    expect(await fetchOccupancyMap(endpoint, { fetcher: refused, decode })).toEqual({ status: 'error' })

    // parseMapMetadata owns the header rules; here it decides the whole read.
    const headerless = vi.fn(async () => pngResponse({ ...HEADERS, 'X-Sweep-Map-Width': '0' }))
    expect(await fetchOccupancyMap(endpoint, { fetcher: headerless, decode })).toEqual({ status: 'error' })

    const undecodable = vi.fn(async () => pngResponse())
    expect(
      await fetchOccupancyMap(endpoint, { fetcher: undecodable, decode: async () => null }),
    ).toEqual({ status: 'error' })

    const offline = vi.fn(async () => {
      throw new Error('network')
    })
    expect(await fetchOccupancyMap(endpoint, { fetcher: offline, decode })).toEqual({ status: 'error' })
  })

  test('reset posts to the reset path and reports whether the relay cleared the grid', async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }))
    expect(await resetOccupancyMap(endpoint, fetcher)).toBe(true)
    expect(fetcher).toHaveBeenCalledWith(endpoint.resetUrl, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: 'Bearer map-token' },
    })
    const refused = vi.fn(async () => new Response(null, { status: 403 }))
    expect(await resetOccupancyMap(endpoint, refused)).toBe(false)
    const offline = vi.fn(async () => {
      throw new Error('network')
    })
    expect(await resetOccupancyMap(endpoint, offline)).toBe(false)
  })
})
