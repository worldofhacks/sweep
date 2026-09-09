import { expect, it, vi } from 'vitest'
import { AtlasClient } from './client'
import { responseMatches } from './captureRequests'
import type { CaptureMetadata, CaptureResponseTarget } from './types'

const surface: CaptureResponseTarget = { kind: 'surface', job_id: '11111111-1111-1111-1111-111111111111', artifact_sha256: 'a'.repeat(64), region_id: '0123456789abcdef' }
const location: CaptureResponseTarget = { kind: 'location', cell_id: '5:5' }

it.each([surface, location])('requires acknowledgment of the exact requested view: $kind', async target => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ id: 'saved' }))
    .mockResolvedValueOnce(Response.json({ id: 'saved', response_to: { ...target, kind: 'other' } }))
    .mockResolvedValueOnce(Response.json({ id: 'saved', response_to: target }))
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'room', token: 'test-only' }, fetcher)
  const file = new File(['original'], 'austin.jpg', { type: 'image/jpeg' })
  const metadata: CaptureMetadata = { contributor_id: 'person-one', name: 'Sam', source: 'import', kind: 'photo', captured_at: null, position: null, note: '', response_to: target }
  await expect(client.upload('space', file, metadata)).rejects.toThrow('not confirmed against this request')
  await expect(client.upload('space', file, metadata)).rejects.toThrow('not confirmed against this request')
  await expect(client.upload('space', file, metadata)).resolves.toMatchObject({ response_to: target })
  for (const [, init] of fetcher.mock.calls) {
    expect(init.body).toBe(file)
    expect(JSON.parse(init.headers['X-Sweep-Capture']).response_to).toEqual(target)
  }
})

it('never treats the same region identifier in another model as the same request', () => {
  expect(responseMatches(surface, { ...surface, job_id: 'another' })).toBe(false)
  expect(responseMatches(surface, { ...surface, artifact_sha256: 'b'.repeat(64) })).toBe(false)
  expect(responseMatches(surface, null)).toBe(false)
  expect(responseMatches(location, { ...location, cell_id: '6:6' })).toBe(false)
})
