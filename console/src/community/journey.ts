import type { Capture, SpaceDetail } from '../atlas/types'

export interface Journey { saved: string[]; contributions: { id: string; requested: boolean }[] }
export const EMPTY_JOURNEY: Journey = { saved: [], contributions: [] }
/** Deliberately private, device-local encouragement, not an identity or reputation ledger. */
export function readJourney(key: string, storage: Pick<Storage, 'getItem'> = localStorage): Journey {
  const raw = storage.getItem(key)
  if (!raw) return { saved: [], contributions: [] }
  const value = JSON.parse(raw) as Journey
  if (!Array.isArray(value.saved) || !Array.isArray(value.contributions)) throw new Error('Invalid journey')
  return {
    saved: [...new Set(value.saved.filter(id => typeof id === 'string' && id.length <= 128))].slice(0, 500),
    contributions: value.contributions.filter(item => item && typeof item.id === 'string' && item.id.length <= 260 && typeof item.requested === 'boolean').slice(0, 2000),
  }
}
export function rememberContributions(previous: Journey, captures: Capture[], contributor: string): Journey {
  const records = new Map(previous.contributions.map(item => [item.id, item]))
  for (const capture of captures) {
    if (capture.contributor_id !== contributor || !/^[a-f0-9]{64}$/.test(capture.sha256)) continue
    // Reimporting the same original in this workspace does not farm points.
    const id = capture.sha256
    const prior = records.get(id)
    records.set(id, { id, requested: Boolean(prior?.requested || capture.response_to) })
  }
  return { ...previous, contributions: [...records.values()].slice(0, 2000) }
}
export function journeyScore(journey: Journey) {
  const originals = new Map(journey.contributions.map(item => [item.id, item]))
  const count = originals.size
  const requests = [...originals.values()].filter(item => item.requested).length
  return { count, requests, points: count * 10 + requests * 5,
    badge: count >= 10 ? 'Picture builder' : count >= 3 ? 'Perspective partner' : count ? 'First perspective' : 'Curious neighbor' }
}
export function contributingNeighbors(detail: SpaceDetail) {
  const neighbors = new Map<string, { name: string; captures: number }>()
  for (const capture of detail.captures) {
    const prior = neighbors.get(capture.contributor_id)
    neighbors.set(capture.contributor_id, { name: capture.name || 'Neighbor', captures: (prior?.captures ?? 0) + 1 })
  }
  return [...neighbors.entries()].map(([id, person]) => ({ id, ...person })).sort((a, b) => b.captures - a.captures)
}
