import { describe, expect, test, vi } from 'vitest'
import { bootstrapMediaConfiguration, loadMediaRuntimeConfiguration } from './runtime-config'

describe('runtime media configuration', () => {
  test('mounts flight controls before runtime media configuration settles', () => {
    const renderConsole = vi.fn()
    const loader = vi.fn(() => new Promise<never>(() => undefined))

    bootstrapMediaConfiguration(renderConsole, loader)

    expect(renderConsole).toHaveBeenCalledOnce()
    expect(renderConsole).toHaveBeenCalledWith()
  })

  test('loads the read-only credential from the production bootstrap endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          media: {
            webrtcOrigin: 'http://ground-station:8889',
            hlsOrigin: 'http://ground-station:8888',
            readerUsername: 'sweep-reader',
            readerPassword: 'runtime-secret',
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(loadMediaRuntimeConfiguration(fetcher)).resolves.toEqual({
      webrtcOrigin: 'http://ground-station:8889',
      hlsOrigin: 'http://ground-station:8888',
      readerUsername: 'sweep-reader',
      readerPassword: 'runtime-secret',
    })
    expect(fetcher).toHaveBeenCalledWith('/runtime-config.json', {
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
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      media: {
        webrtcOrigin,
        hlsOrigin: 'HTTP://GROUND-STATION:80/',
        readerUsername: 'sweep-reader',
        readerPassword: 'secret',
      },
    }), { status: 200 }))

    await expect(loadMediaRuntimeConfiguration(fetcher, report)).resolves.toBeUndefined()
    expect(report).toHaveBeenCalledOnce()
  })

  test('normalizes trusted origins before playback', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      media: {
        webrtcOrigin: 'HTTPS://GROUND-STATION:443/',
        hlsOrigin: 'HTTP://GROUND-STATION:80/',
        readerUsername: 'sweep-reader',
        readerPassword: 'secret',
      },
    }), { status: 200 }))

    await expect(loadMediaRuntimeConfiguration(fetcher)).resolves.toMatchObject({
      webrtcOrigin: 'https://ground-station',
      hlsOrigin: 'http://ground-station',
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
