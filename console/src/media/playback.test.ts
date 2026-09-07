/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/playback.test.ts)
 * and reduced to the WHEP request. Reconcile when #68 merges.
 */
import { describe, expect, test } from 'vitest'
import { createPlaybackDescriptor, streamName } from './playback'

const credentials = {
  webrtcOrigin: 'http://ground-station:8889/',
  readerUsername: 'console-reader',
  readerPassword: 'read-secret',
}

describe('Media playback handoff', () => {
  test('derives the canonical stream name from the global device id', () => {
    expect(streamName({ drone_id: 1 })).toBe('drone1')
    expect(streamName({ drone_id: 11 })).toBe('drone11')
    expect(streamName({ drone_id: 64 })).toBe('drone64')
  })

  test('provides an authenticated WHEP request for the derived stream', () => {
    const descriptor = createPlaybackDescriptor({
      device: { drone_id: 4 },
      ...credentials,
    })

    expect(descriptor).toEqual({
      stream: 'drone4',
      primary: {
        protocol: 'whep',
        url: 'http://ground-station:8889/drone4/whep',
        authorization: `Basic ${btoa('console-reader:read-secret')}`,
      },
    })
  })

  test('plays the explicitly configured camera without changing its device identity', () => {
    expect(createPlaybackDescriptor({
      ...credentials, device: { drone_id: 11 }, stream: 'robot-five-rear',
    })).toEqual({
      stream: 'robot-five-rear', primary: {
        protocol: 'whep', url: 'http://ground-station:8889/robot-five-rear/whep',
        authorization: `Basic ${btoa('console-reader:read-secret')}`,
      },
    })
  })

  test.each(['', '../other', '//other-host', 'https://other-host/video', 'camera?token=x', 'camera%2fother'])('rejects an arbitrary camera path %s', (stream) => {
    expect(() => createPlaybackDescriptor({
      ...credentials, device: { drone_id: 11 }, stream,
    })).toThrow('Invalid configured camera stream name')
  })

  test('a physical ground vehicle uses its global device path', () => {
    expect(
      createPlaybackDescriptor({ device: { drone_id: 12 }, ...credentials })
        .primary.url,
    ).toBe('http://ground-station:8889/drone12/whep')
    expect(
      createPlaybackDescriptor({ device: { drone_id: 1 }, ...credentials }).stream,
    ).toBe('drone1')
  })

  test.each([0, -1, 1.5, 65])('rejects invalid device id %s', (drone_id) => {
    expect(() =>
      createPlaybackDescriptor({
        device: { drone_id },
        webrtcOrigin: 'http://localhost:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('drone_id must be an integer from 1 through 64')
  })

  test('rejects an empty read credential', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { drone_id: 1 },
        webrtcOrigin: 'http://localhost:8889',
        readerUsername: 'reader',
        readerPassword: '',
      }),
    ).toThrow('Media reader credentials are required')
  })

  test('rejects non-HTTP media origins', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { drone_id: 1 },
        webrtcOrigin: 'javascript:alert(1)',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must use HTTP or HTTPS')
  })

  test('rejects an origin that carries its own credentials', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { drone_id: 1 },
        webrtcOrigin: 'http://reader:secret@localhost:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must not contain credentials')
  })
})
