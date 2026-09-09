import { expect, it } from 'vitest'
import { EMPTY_JOURNEY, journeyScore, readJourney, rememberContributions } from './journey'
import { COMMUNITY_EXAMPLES } from './examples'
import type { Capture } from '../atlas/types'

const original: Capture = { id: 'capture-one', contributor_id: 'neighbor-one', name: 'Neighbor', kind: 'photo',
  source: 'import', captured_at: null, position: null, note: '', uploaded_at: 1, bytes: 20, mime: 'image/png', sha256: 'a'.repeat(64) }

it('counts only this contributor’s confirmed originals, with no GPS or reporting incentive', () => {
  const journey = rememberContributions(EMPTY_JOURNEY, [original, { ...original, contributor_id: 'someone-else', sha256: 'b'.repeat(64) }, { ...original, sha256: '' }], 'neighbor-one')
  expect(journeyScore(journey)).toEqual({ points: 10, count: 1, requests: 0, badge: 'First perspective' })
  expect(original.position).toBeNull()
})

it('deduplicates repeated originals and awards the request bonus only once', () => {
  const first = rememberContributions(EMPTY_JOURNEY, [original], 'neighbor-one')
  const response = { ...original, id: 'capture-two', response_to: { kind: 'location' as const, cell_id: '1:1' } }
  const next = rememberContributions(first, [original, response, response], 'neighbor-one')
  expect(journeyScore(next).points).toBe(15)
  expect(rememberContributions(next, [original], 'neighbor-one')).toEqual(next)
})

it('does not reward bookmarks, empty state, or another person’s contributions', () => {
  expect(journeyScore({ saved: ['space'], contributions: [] }).points).toBe(0)
  expect(journeyScore(rememberContributions(EMPTY_JOURNEY, [original], 'someone-else')).count).toBe(0)
})

it('validates and bounds device-local stored data instead of trusting arbitrary JSON', () => {
  expect(readJourney('new', { getItem: () => null })).toEqual(EMPTY_JOURNEY)
  expect(() => readJourney('broken', { getItem: () => '{}' })).toThrow()
  expect(readJourney('saved', { getItem: () => JSON.stringify({ saved: ['a', 'a', 10], contributions: [null, { id: 'a', requested: false }] }) })).toEqual({ saved: ['a'], contributions: [{ id: 'a', requested: false }] })
})

it('keeps editorial starters in Austin with concrete views and no invented capture counts', () => {
  expect(COMMUNITY_EXAMPLES).toHaveLength(3)
  for (const example of COMMUNITY_EXAMPLES) {
    expect(example.space.place).toContain('Austin, Texas')
    expect(example.space.latitude).toBeGreaterThan(30.2)
    expect(example.space.latitude).toBeLessThan(30.4)
    expect(example.space.longitude).toBeGreaterThan(-97.8)
    expect(example.space.longitude).toBeLessThan(-97.6)
    expect(example.views).toHaveLength(3)
    expect(example.space).not.toHaveProperty('capture_count')
    expect(example.space.category).not.toBe('incident')
  }
})
