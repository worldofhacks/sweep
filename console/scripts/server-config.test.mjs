import { execFileSync } from 'node:child_process'
import { describe, expect, test } from 'vitest'
import { hostHeaderIsAllowed } from './host-validation.mjs'
import { readConsoleServerConfiguration } from './server-config.mjs'

describe('production console server configuration', () => {
  test('keeps the local listener separate from the browser-visible origin', () => {
    const configuration = readConsoleServerConfiguration({
      SWEEP_CONSOLE_BIND_HOST: '127.0.0.1',
      SWEEP_CONSOLE_PORT: '5173',
      SWEEP_CONSOLE_ORIGIN: 'https://ground-station.example',
    })

    expect(configuration).toMatchObject({
      bindHost: '127.0.0.1',
      bindPort: 5173,
      publicOrigin: 'https://ground-station.example',
    })
  })

  test.each([
    'https://ground-station.example:443',
    'http://ground-station.example:80',
  ])('rejects a non-canonical public origin before it can drift from MediaMTX CORS: %s', (origin) => {
    expect(() => readConsoleServerConfiguration({ SWEEP_CONSOLE_ORIGIN: origin })).toThrow(
      'SWEEP_CONSOLE_ORIGIN must be a canonical origin',
    )
  })

  test('requires an explicit public origin when the listener leaves the standard port', () => {
    expect(() => readConsoleServerConfiguration({ SWEEP_CONSOLE_PORT: '80' })).toThrow(
      'SWEEP_CONSOLE_ORIGIN is required when SWEEP_CONSOLE_PORT differs from 5173',
    )
  })

  test('uses the same standard origin as Compose when no origin is configured', () => {
    expect(readConsoleServerConfiguration({}).publicOrigin).toBe('http://localhost:5173')
  })

  test.each([
    ['http://ground-station.example', 'ground-station.example'],
    ['https://ground-station.example', 'ground-station.example'],
    ['https://ground-station.example:8443', 'ground-station.example:8443'],
  ])('passes canonical origin %s unchanged to Host validation and MediaMTX CORS', (origin, host) => {
    const configuration = readConsoleServerConfiguration({ SWEEP_CONSOLE_ORIGIN: origin })
    const compose = execFileSync(
      'docker',
      ['compose', '--file', '../docker-compose.yml', 'config'],
      {
        encoding: 'utf8',
        env: {
          ...process.env,
          SWEEP_CONSOLE_ORIGIN: origin,
          SWEEP_MEDIA_ADMIN_PASSWORD: 'admin-secret',
          SWEEP_MEDIA_READ_PASSWORD: 'reader-secret',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_1: 'publisher-1',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_2: 'publisher-2',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_3: 'publisher-3',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_4: 'publisher-4',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_5: 'publisher-5',
          SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_6: 'publisher-6',
        },
      },
    )

    expect(configuration.publicOrigin).toBe(origin)
    expect(hostHeaderIsAllowed(host, configuration.publicOrigin)).toBe(true)
    expect(compose).toContain(`MTX_HLSALLOWORIGINS: ${origin}`)
    expect(compose).toContain(`MTX_WEBRTCALLOWORIGINS: ${origin}`)
  })

  test('serves the console with media disabled when media variables are absent', () => {
    expect(readConsoleServerConfiguration({}).media).toBeUndefined()
    expect(readConsoleServerConfiguration({
      SWEEP_MEDIA_WEBRTC_ORIGIN: 'http://localhost:8889',
      SWEEP_MEDIA_READ_PASSWORD: 'partial-secret',
    }).media).toBeUndefined()
  })

  test('normalizes valid media origins and rejects unsafe ones', () => {
    const base = {
      SWEEP_MEDIA_HLS_ORIGIN: 'HTTP://GROUND-STATION.local:80/',
      SWEEP_MEDIA_READ_USERNAME: 'sweep-reader',
      SWEEP_MEDIA_READ_PASSWORD: 'runtime-secret',
    }

    expect(readConsoleServerConfiguration({
      ...base,
      SWEEP_MEDIA_WEBRTC_ORIGIN: 'https://GROUND-STATION.local:443/',
    }).media).toEqual({
      webrtcOrigin: 'https://ground-station.local',
      hlsOrigin: 'http://ground-station.local',
      readerUsername: 'sweep-reader',
      readerPassword: 'runtime-secret',
    })
    expect(readConsoleServerConfiguration({
      ...base,
      SWEEP_MEDIA_WEBRTC_ORIGIN: 'https://user:secret@ground-station.local',
    }).media).toBeUndefined()
    expect(readConsoleServerConfiguration({
      ...base,
      SWEEP_MEDIA_WEBRTC_ORIGIN: 'ftp://ground-station.local',
    }).media).toBeUndefined()
  })
})
