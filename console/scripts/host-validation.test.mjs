import { describe, expect, test } from 'vitest'
import { hostHeaderIsAllowed } from './host-validation.mjs'

describe('production console Host validation', () => {
  test.each([
    ['localhost:5173', 'http://localhost:5173'],
    ['GROUND-STATION.local:8443', 'https://ground-station.local:8443'],
    ['[::1]:5173', 'http://[::1]:5173'],
    ['ground-station.local', 'http://ground-station.local:80'],
    ['ground-station.local:80', 'http://ground-station.local:80'],
    ['ground-station.local', 'https://ground-station.local:443'],
  ])('accepts the configured authority %s', (header, origin) => {
    expect(hostHeaderIsAllowed(header, origin)).toBe(true)
  })

  test.each([
    undefined,
    'attacker.example:5173',
    'localhost:9999',
    'localhost:5173, attacker.example',
  ])('rejects an unconfigured Host header: %s', (header) => {
    expect(hostHeaderIsAllowed(header, 'http://localhost:5173')).toBe(false)
  })
})
