import type { MapEndpoint } from '../relay/map-endpoint'

// Synthetic room raster for the mixed fixture. This transport never contacts a relay.
const ROOM_PNG = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAAAOElEQVR4nO3WsQ0AIBADsYzO5lCwARHNyzeA68sqCwAwBkjRBfZzAAAAAAAAAIB/QLt5XQDAEOAARfPkChBcZQgAAAAASUVORK5CYII="
export function fixtureMapEndpoint(now: () => number): MapEndpoint {
  return {
    url: 'https://fixture.invalid/api/sessions/mixed/map',
    resetUrl: 'https://fixture.invalid/api/sessions/mixed/map/reset',
    authorization: '',
    fetcher: async (_input, init) => {
      if (init?.method === 'POST') return new Response(null, { status: 204 })
      const bytes = Uint8Array.from(atob(ROOM_PNG), (char) => char.charCodeAt(0))
      return new Response(bytes, { headers: {
        'Content-Type': 'image/png',
        'X-Sweep-Map-Resolution-M': '0.1',
        'X-Sweep-Map-Origin-X': '-3.2',
        'X-Sweep-Map-Origin-Y': '-3.2',
        'X-Sweep-Map-Width': '64',
        'X-Sweep-Map-Height': '64',
        'X-Sweep-Map-Updated-At': String(now()),
      } })
    },
  }
}
