import { parseMapMetadata, type MapMetadata } from '../../relay/contract'
import type { MapEndpoint } from '../../relay/map-endpoint'

/**
 * One read of `GET /api/sessions/{id}/map`: the PNG the relay encoded plus the
 * grid geometry from its headers. Row 0 of the image is the top of the grid
 * (maximum y) and `origin_x`/`origin_y` are the world coordinates of the
 * bottom-left cell corner.
 */
export interface OccupancyMap extends MapMetadata {
  image: CanvasImageSource
}

/**
 * `absent` is a relay that has no grid for this session yet (404); `error` is
 * anything else, including headers that do not describe a grid. Neither
 * invents a raster.
 */
export type OccupancyResult =
  | { status: 'map'; map: OccupancyMap }
  | { status: 'absent' }
  | { status: 'error' }

export type ImageDecoder = (blob: Blob) => Promise<CanvasImageSource | null>

/** Browsers decode the PNG off the main thread; anything else draws no raster. */
export const decodeMapImage: ImageDecoder = async (blob) => {
  if (typeof createImageBitmap !== 'function') return null
  try {
    return await createImageBitmap(blob)
  } catch {
    return null
  }
}

export interface FetchMapOptions {
  fetcher?: typeof fetch
  decode?: ImageDecoder
  signal?: AbortSignal
}

export async function fetchOccupancyMap(
  endpoint: MapEndpoint,
  { fetcher = endpoint.fetcher ?? fetch, decode = decodeMapImage, signal }: FetchMapOptions = {},
): Promise<OccupancyResult> {
  let response: Response
  try {
    response = await fetcher(endpoint.url, {
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: endpoint.authorization },
      signal,
    })
  } catch {
    return { status: 'error' }
  }
  if (response.status === 404) return { status: 'absent' }
  if (!response.ok) return { status: 'error' }
  const metadata = parseMapMetadata((name) => response.headers.get(name))
  if (!metadata) return { status: 'error' }
  try {
    const image = await decode(await response.blob())
    if (!image) return { status: 'error' }
    return { status: 'map', map: { ...metadata, image } }
  } catch {
    return { status: 'error' }
  }
}

/** Clears the grid; false when the relay refused or could not be reached. */
export async function resetOccupancyMap(
  endpoint: MapEndpoint,
  fetcher: typeof fetch = endpoint.fetcher ?? fetch,
): Promise<boolean> {
  try {
    const response = await fetcher(endpoint.resetUrl, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: endpoint.authorization },
    })
    return response.ok
  } catch {
    return false
  }
}
