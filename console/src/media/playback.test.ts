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
  test('derives the stream name from the device class and unit alone', () => {
    expect(streamName({ device_class: 'aircraft', unit: 1 })).toBe('drone1')
    expect(streamName({ device_class: 'aircraft', unit: 6 })).toBe('drone6')
    expect(streamName({ device_class: 'ground_vehicle', unit: 1 })).toBe('ground1')
    expect(streamName({ device_class: 'ground_vehicle', unit: 3 })).toBe('ground3')
  })

  test('provides an authenticated WHEP request for the derived stream', () => {
    const descriptor = createPlaybackDescriptor({
      device: { device_class: 'aircraft', unit: 4 },
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

  test('a ground vehicle plays its ground path, and any positive unit is accepted', () => {
    expect(
      createPlaybackDescriptor({ device: { device_class: 'ground_vehicle', unit: 2 }, ...credentials })
        .primary.url,
    ).toBe('http://ground-station:8889/ground2/whep')
    expect(
      createPlaybackDescriptor({ device: { device_class: 'aircraft', unit: 7 }, ...credentials }).stream,
    ).toBe('drone7')
  })

  test.each([0, -1, 1.5])('rejects invalid unit %s', (unit) => {
    expect(() =>
      createPlaybackDescriptor({
        device: { device_class: 'aircraft', unit },
        webrtcOrigin: 'http://localhost:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('unit must be a positive integer')
  })

  test('rejects an empty read credential', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { device_class: 'aircraft', unit: 1 },
        webrtcOrigin: 'http://localhost:8889',
        readerUsername: 'reader',
        readerPassword: '',
      }),
    ).toThrow('Media reader credentials are required')
  })

  test('rejects non-HTTP media origins', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { device_class: 'aircraft', unit: 1 },
        webrtcOrigin: 'javascript:alert(1)',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must use HTTP or HTTPS')
  })

  test('rejects an origin that carries its own credentials', () => {
    expect(() =>
      createPlaybackDescriptor({
        device: { device_class: 'aircraft', unit: 1 },
        webrtcOrigin: 'http://reader:secret@localhost:8889',
        readerUsername: 'reader',
        readerPassword: 'secret',
      }),
    ).toThrow('Media origins must not contain credentials')
  })
})
