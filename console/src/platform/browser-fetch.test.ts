import { expect, test, vi } from 'vitest'
import { HttpSearchClient } from '../search/client'
import { PlatformHttp } from './http'

test('default HTTP clients call browser fetch without a client object receiver', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async function (this: unknown) {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation')
    return new Response(JSON.stringify({ session: 'test-session', target_classes: ['backpack'], zones: ['lobby'] }))
  })
  try {
    const connection = { baseUrl: 'http://relay.test', sessionId: 'test-session', token: 'test-token' }
    await expect(new PlatformHttp(connection).request('/platform')).resolves.toMatchObject({ session: 'test-session' })
    await expect(new HttpSearchClient(connection).catalog('test-session')).resolves.toEqual({ target_classes: ['backpack'], zones: ['lobby'] })
    expect(fetcher).toHaveBeenCalledTimes(2)
  } finally { fetcher.mockRestore() }
})
