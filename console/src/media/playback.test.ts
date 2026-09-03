import { describe, expect, test } from 'vitest'
import { createPlaybackDescriptor } from './playback'

describe('Media playback handoff', () => {
  test('provides authenticated WHEP primary and HLS fallback requests', () => {
    const descriptor = createPlaybackDescriptor({
      droneId: 4,
      webrtcOrigin: 'http://ground-station:8889/',
      hlsOrigin: 'http://ground-station:8888/',
      readerUsername: 'console-reader',
      readerPassword: 'read-secret',
    })

    expect(descriptor).toEqual({
      stream: 'drone4',
      primary: {
        protocol: 'whep',
        url: 'http://ground-station:8889/drone4/whep',
        authorization: `Basic ${btoa('console-reader:read-secret')}`,
      },
      fallback: {
        protocol: 'hls',
        url: 'http://ground-station:8888/drone4/index.m3u8',
        authorization: `Basic ${btoa('console-reader:read-secret')}`,
      },
    })
  })

  test.each([0, 7, 1.5])('rejects invalid drone id %s', (droneId) => {
    expect(() =>
      createPlaybackDescriptor({
        droneId,
        webrtcOrigin: 'http://localhost:8889',
        hlsOrigin: 'http://localhost:8888',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('droneId must be an integer from 1 through 6')
  })

  test('rejects an empty read credential', () => {
    expect(() =>
      createPlaybackDescriptor({
        droneId: 1,
        webrtcOrigin: 'http://localhost:8889',
        hlsOrigin: 'http://localhost:8888',
        readerUsername: 'reader',
        readerPassword: '',
      }),
    ).toThrow('Media reader credentials are required')
  })

  test('rejects non-HTTP media origins', () => {
    expect(() =>
      createPlaybackDescriptor({
        droneId: 1,
        webrtcOrigin: 'javascript:alert(1)',
        hlsOrigin: 'http://localhost:8888',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must use HTTP or HTTPS')
  })
})
