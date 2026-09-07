import { expect, test } from 'vitest'
import { canDraw, emptyDraft, geometryIssue, imageCoordinates, validateDraft, worldCoordinates } from './geometry'
import { parseLocalDraft } from './files'
import { draftFixture, tagFixture } from './test-fixtures'

test('an empty runtime draft has no generated map, metadata, tags, or approval', () => {
  const draft = emptyDraft()
  expect(draft.image).toBeNull()
  expect(draft.features).toEqual([])
  expect(draft.tags).toEqual([])
  expect(canDraw(draft)).toBe(false)
  expect(validateDraft(draft).map((i) => i.path)).toEqual(expect.arrayContaining(['image', 'metadata.frame', 'metadata.origin', 'geofence']))
})

test('coordinates use the measured origin/resolution and flip image y once', () => {
  const draft = draftFixture()
  draft.metadata.originXM = -2
  draft.metadata.originYM = 3
  const world = worldCoordinates(draft, { x: 40, y: 20 })
  expect(world).toEqual({ x: 2, y: 11 })
  expect(imageCoordinates(draft, world)).toEqual({ x: 40, y: 20 })
})

test.each([
  [[{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 0, y: 2 }], 'closure'],
  [[{ x: 0, y: 0 }, { x: 2, y: 2 }, { x: 0, y: 2 }, { x: 2, y: 0 }, { x: 0, y: 0 }], 'itself'],
  [[{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 0 }, { x: 0, y: 0 }], 'zero area'],
  [[{ x: 0, y: 0 }, { x: Infinity, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 0 }], 'finite'],
] as const)('rejects invalid polygon geometry %#', (points, message) => {
  expect(geometryIssue([...points], true)).toContain(message)
})

test('corridor width and hand-measured height evidence are required independently of the image', () => {
  const draft = draftFixture()
  draft.features.push({ ...draft.features[0], id: 'route', name: 'Test corridor', kind: 'corridor', points: [{ x: 2, y: 2 }, { x: 8, y: 2 }] })
  expect(validateDraft(draft).filter((i) => i.path === 'features.route').map((i) => i.message)).toEqual(expect.arrayContaining([
    'Corridor width must be positive.', 'Enter a hand-measured flight height and positive tolerance.',
    'Hand-measured flight-height evidence is required; LiDAR-plane height is insufficient.',
  ]))
  Object.assign(draft.features[1], { widthM: 1, flightHeightM: 1.5, heightToleranceM: 0.1, heightEvidence: 'Test-only measured clearance record' })
  expect(validateDraft(draft)).toEqual([])
  draft.features[1].widthM = 5
  expect(validateDraft(draft).some((i) => i.message.includes('inside the geofence'))).toBe(true)
})

test('flight tags require actual draft verification evidence, and duplicate identities are refused', () => {
  const draft = draftFixture()
  draft.tags = [{ ...tagFixture(), tapeVerified: false, observations: [] }]
  expect(validateDraft(draft).some((i) => i.message.includes('Flight tags require'))).toBe(true)
  draft.tags = [tagFixture(), { ...tagFixture(), id: 'other' }]
  expect(validateDraft(draft).some((i) => i.message === 'Duplicate tag ID.')).toBe(true)
  draft.metadata.frame = 'building'
  expect(canDraw(draft)).toBe(false)
})

test('local import drops forged approval and validation fields and rejects unsafe image URLs', () => {
  const draft = draftFixture()
  const imported = parseLocalDraft(JSON.stringify({ ...draft, approved: true, validation: { valid: true }, approval: { auditId: 'forged' } }))
  expect(imported).toEqual(draft)
  expect(imported).not.toHaveProperty('approval')
  draft.image!.dataUrl = 'https://unconfigured.example/image.png'
  expect(() => parseLocalDraft(JSON.stringify(draft))).toThrow('Invalid or oversized')
})

test('local drafts roundtrip measured geometry and evidence without creating authority', () => {
  const draft = draftFixture()
  draft.tags = [tagFixture()]
  expect(parseLocalDraft(JSON.stringify(draft))).toEqual(draft)
  expect(validateDraft(draft)).toEqual([])
})
