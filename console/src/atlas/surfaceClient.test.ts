import { expect, test, vi } from 'vitest'
import { AtlasClient } from './client'

const region = { id: 'region', segments: [[1, 2, 3], [4, 5, 6]] }

test.each(['job', 'checksum', 'region', 'segments'])('refuses a %s mismatch in lazy review geometry', async fault => {
  const manifest = { job_id: 'job', artifact_sha256: 'checksum', surface_review: { regions: [region] } }
  if (fault === 'job') manifest.job_id = 'other'
  if (fault === 'checksum') manifest.artifact_sha256 = 'other'
  if (fault === 'region') manifest.surface_review = { regions: [{ ...region, id: 'other' }] }
  if (fault === 'segments') manifest.surface_review = { regions: [{ ...region, segments: [] }] }
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'scope', token: 'local-test' })
  vi.spyOn(client.http, 'request').mockResolvedValue(manifest)
  await expect(client.surfaceRegion('space', 'job', 'checksum', 'region', new AbortController().signal)).rejects.toThrow('could not be verified')
})

test('loads review geometry only through the authenticated immutable manifest endpoint', async () => {
  const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'scope', token: 'local-test' })
  const read = vi.spyOn(client.http, 'request').mockResolvedValue({ job_id: 'job', artifact_sha256: 'checksum', surface_review: { regions: [region] } })
  const signal = new AbortController().signal
  expect(await client.surfaceRegion('space', 'job', 'checksum', 'region', signal)).toEqual(region)
  expect(read).toHaveBeenCalledExactlyOnceWith('/atlas/spaces/space/reconstruction/job/manifest.json', undefined, signal)
})
