import { describe, expect, test } from 'vitest'
import { assertConsoleEndpoint, expectedServerPort } from './vite.config'

const canonicalEndpoint = { host: '127.0.0.1', port: 5173, strictPort: true }

describe('single-console port guard', () => {
  test('admits the m14 browser runner only at its isolated loopback port', () => {
    const testPort = expectedServerPort({ SWEEP_CONSOLE_TEST_MODE: 'm14-browser' })
    expect(testPort).toBe(14173)
    expect(() => assertConsoleEndpoint({ ...canonicalEndpoint, port: testPort }, testPort)).not.toThrow()
    expect(() => assertConsoleEndpoint(canonicalEndpoint, testPort)).toThrow('one laptop console')
  })

  test('keeps ordinary development on the canonical loopback endpoint', () => {
    const port = expectedServerPort({})
    expect(port).toBe(5173)
    expect(() => assertConsoleEndpoint(canonicalEndpoint, port)).not.toThrow()
    expect(() => assertConsoleEndpoint({ ...canonicalEndpoint, host: '0.0.0.0' }, port)).toThrow('one laptop console')
  })
})
