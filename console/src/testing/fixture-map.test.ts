import { expect, test } from 'vitest'
import { fixtureMapEndpoint } from './fixture-map'
import { fetchOccupancyMap, resetOccupancyMap } from '../modules/map/occupancy'

test('mixed fixture supplies a real grayscale PNG through the same map transport and reset path', async () => {
  const endpoint = fixtureMapEndpoint(() => 123456)
  const response = await endpoint.fetcher!(endpoint.url)
  const bytes = new Uint8Array(await response.arrayBuffer())
  expect([...bytes.slice(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10])
  expect(bytes[25]).toBe(0) // grayscale PNG colour type
  const result = await fetchOccupancyMap(endpoint, { decode: async () => ({ width: 64, height: 64 } as CanvasImageSource) })
  expect(result).toMatchObject({ status: 'map', map: { width: 64, height: 64, resolution_m: 0.1, origin_x: -3.2, origin_y: -3.2, updated_at: 123456 } })
  expect(await resetOccupancyMap(endpoint)).toBe(true)
})
