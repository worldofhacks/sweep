/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/runtime-config.test.ts);
 * the HLS origin is no longer part of the configuration. Reconcile when #68 merges.
 */
import { describe, expect, test, vi } from 'vitest'
import {
  MEDIA_CONFIG_ENDPOINT,
  bootstrapMediaConfiguration,
  loadMediaRuntimeConfiguration,
  normalizeMediaConfiguration,
} from './runtime-config'

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
