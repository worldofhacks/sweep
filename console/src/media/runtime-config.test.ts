/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/runtime-config.test.ts);
 * the HLS origin is no longer part of the configuration. Reconcile when #68 merges.
 */
import { describe, expect, test, vi } from 'vitest'
import {
  MEDIA_CONFIG_ENDPOINT,
  SAME_ORIGIN_MEDIA_SOURCE,
  bootstrapMediaConfiguration,
  loadMediaRuntimeConfiguration,
  normalizeMediaConfiguration,
  relayMediaConfigurationSource,
} from './runtime-config'

const media = {
  webrtcOrigin: 'http://ground-station:8889',
  readerUsername: 'sweep-reader',
  readerPassword: 'runtime-secret',
}
const relaySource = {
  url: 'http://ground-station:8000/runtime-config.json',
  authorization: 'Bearer relay-token',
}

describe('relay media configuration source', () => {
  test('maps the relay WebSocket origin to its HTTP endpoint behind the bearer', () => {
    expect(relayMediaConfigurationSource('ws://ground-station:8000/ws/demo', 'relay-token')).toEqual(
      relaySource,
    )
    expect(relayMediaConfigurationSource('wss://relay.example', 'relay-token')).toEqual({
      url: 'https://relay.example/runtime-config.json',
      authorization: 'Bearer relay-token',
    })
  })

  test.each([
    ['an unparseable URL', 'not a url', 'relay-token'],
    ['an unsupported protocol', 'ftp://ground-station', 'relay-token'],
    ['an empty token', 'ws://ground-station:8000', ''],
  ])('yields no source for %s', (_name, baseUrl, token) => {
    expect(relayMediaConfigurationSource(baseUrl, token)).toBeNull()
  })
})

describe('media configuration fallback to the relay', () => {
  test('reads the relay endpoint with the bearer only after the same-origin endpoint fails', async () => {
    const report = vi.fn()
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response('<!doctype html><title>Sweep</title>', { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ media }), { status: 200 }))

    await expect(
      loadMediaRuntimeConfiguration(fetcher, report, [SAME_ORIGIN_MEDIA_SOURCE, relaySource]),
    ).resolves.toEqual(media)

    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher).toHaveBeenNthCalledWith(1, MEDIA_CONFIG_ENDPOINT, {
      cache: 'no-store',
      credentials: 'same-origin',
    })
    expect(fetcher).toHaveBeenNthCalledWith(2, relaySource.url, {
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: 'Bearer relay-token' },
    })
    expect(report).not.toHaveBeenCalled()
  })

  test('never contacts the relay when the same-origin endpoint answers', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ media }), { status: 200 }))

    await expect(
      loadMediaRuntimeConfiguration(fetcher, vi.fn(), [SAME_ORIGIN_MEDIA_SOURCE, relaySource]),
    ).resolves.toEqual(media)
    expect(fetcher).toHaveBeenCalledOnce()
  })

  test('reports once and leaves playback disabled when every source fails', async () => {
    const report = vi.fn()
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(new Response(JSON.stringify({ media: null }), { status: 503 }))

    await expect(
      loadMediaRuntimeConfiguration(fetcher, report, [SAME_ORIGIN_MEDIA_SOURCE, relaySource]),
    ).resolves.toBeUndefined()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(report).toHaveBeenCalledOnce()
  })
})

describe('runtime media configuration', () => {
  test('mounts flight controls before runtime media configuration settles', () => {
    const renderConsole = vi.fn()
    const loader = vi.fn(() => new Promise<never>(() => undefined))

    bootstrapMediaConfiguration(renderConsole, loader)

    expect(renderConsole).toHaveBeenCalledOnce()
    expect(renderConsole).toHaveBeenCalledWith()
  })

  test('renders once more with the configuration when the loader provides one', async () => {
    const renderConsole = vi.fn()
    const configuration = {
      webrtcOrigin: 'http://ground-station:8889',
      readerUsername: 'sweep-reader',
      readerPassword: 'runtime-secret',
    }

    bootstrapMediaConfiguration(renderConsole, () => Promise.resolve(configuration))
    await vi.waitFor(() => expect(renderConsole).toHaveBeenCalledTimes(2))

    expect(renderConsole).toHaveBeenLastCalledWith(configuration)
  })

  test('loads the read-only credential from the production bootstrap endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          media: {
            webrtcOrigin: 'http://ground-station:8889',
            readerUsername: 'sweep-reader',
            readerPassword: 'runtime-secret',
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(loadMediaRuntimeConfiguration(fetcher)).resolves.toEqual({
      webrtcOrigin: 'http://ground-station:8889',
      readerUsername: 'sweep-reader',
      readerPassword: 'runtime-secret',
    })
    expect(fetcher).toHaveBeenCalledWith(MEDIA_CONFIG_ENDPOINT, {
      cache: 'no-store',
      credentials: 'same-origin',
    })
  })

  test('leaves playback disabled when the runtime endpoint is unavailable', async () => {
    const report = vi.fn()
    const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 503 }))

    await expect(loadMediaRuntimeConfiguration(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledWith('Media playback disabled: runtime configuration unavailable.')
  })

  test('degrades incomplete bootstrap data instead of constructing an unsafe request', async () => {
    const report = vi.fn()
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ media: { readerPassword: 'secret' } }), { status: 200 }),
    )

    await expect(loadMediaRuntimeConfiguration(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
  })

  test.each([
    ['invalid URL', 'not a URL'],
    ['unsupported protocol', 'ftp://ground-station'],
    ['embedded credentials', 'https://reader:secret@ground-station'],
    ['non-origin path', 'https://ground-station/media'],
  ])('degrades a complete configuration with an %s', async (_name, webrtcOrigin) => {
    const report = vi.fn()
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          media: { webrtcOrigin, readerUsername: 'sweep-reader', readerPassword: 'secret' },
        }),
        { status: 200 },
      ),
    )

    await expect(loadMediaRuntimeConfiguration(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
  })

  test('normalizes a trusted origin and ignores an HLS origin in the payload', () => {
    expect(
      normalizeMediaConfiguration({
        webrtcOrigin: 'HTTPS://GROUND-STATION:443/',
        hlsOrigin: 'http://ground-station:8888',
        readerUsername: 'sweep-reader',
        readerPassword: 'secret',
      }),
    ).toEqual({
      webrtcOrigin: 'https://ground-station',
      readerUsername: 'sweep-reader',
      readerPassword: 'secret',
    })
  })

  test.each([
    ['network rejection', vi.fn().mockRejectedValue(new TypeError('offline'))],
    ['malformed JSON', vi.fn().mockResolvedValue(new Response('{', { status: 200 }))],
  ])('degrades %s without blocking the control console', async (_name, fetcher) => {
    const report = vi.fn()

    await expect(loadMediaRuntimeConfiguration(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
  })
})
