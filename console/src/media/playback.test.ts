/**
 * Ported from PR #68 (feat/m31-media-ingest, console/src/media/playback.test.ts)
 * and reduced to the WHEP request. Reconcile when #68 merges.
 */
import { describe, expect, test } from 'vitest'
import { createPlaybackDescriptor, streamName } from './playback'

describe('Media playback handoff', () => {
  test('derives the stream name from the aircraft id alone', () => {
    expect(streamName(1)).toBe('drone1')
    expect(streamName(6)).toBe('drone6')
  })

  test('provides an authenticated WHEP request for the derived stream', () => {
    const descriptor = createPlaybackDescriptor({
      droneId: 4,
      webrtcOrigin: 'http://ground-station:8889/',
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
    })
  })

  test.each([0, 7, 1.5])('rejects invalid drone id %s', (droneId) => {
    expect(() =>
      createPlaybackDescriptor({
        droneId,
        webrtcOrigin: 'http://localhost:8889',
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
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must use HTTP or HTTPS')
  })

  test('rejects an origin that carries its own credentials', () => {
    expect(() =>
      createPlaybackDescriptor({
        droneId: 1,
        webrtcOrigin: 'http://reader:secret@localhost:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must not contain credentials')
  })
})
