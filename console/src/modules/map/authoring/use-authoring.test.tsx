import { act, renderHook } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { UNAVAILABLE_MAP_AUTHORING_CLIENT, type MapAuthoringClient } from './client'
import { useMapAuthoring } from './use-authoring'
import { clientFixture, deferred, draftFixture, nextRevision, revision, rosterFixture, tagFixture } from './test-fixtures'
import type { CurrentTagObservation, MapApproval, MapDraft, MapValidation, RevisionComparison } from './types'
import { verifyDraftImage } from './files'

const now = () => 10_000
vi.mock('./files', async (load) => ({ ...await load<typeof import('./files')>(), verifyDraftImage: vi.fn(async (draft: MapDraft) => draft) }))

test('create, edit, undo, save, validate and approve bind every request to an exact immutable revision', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now))
  act(() => result.current.replace(draftFixture()))
  act(() => result.current.changed({ ...result.current.draft, metadata: { ...result.current.draft.metadata, mapVersion: 'edited-v2' } }))
  expect(result.current.dirty).toBe(true)
  act(() => result.current.undo())
  expect(result.current.draft.metadata.mapVersion).toBe('test-map-v1')
  await act(() => result.current.save())
  expect(client.save).toHaveBeenCalledWith(draftFixture(), null)
  expect(result.current.canApprove).toBe(false)
  await act(() => result.current.validate())
  expect(result.current.canApprove).toBe(true)
  await act(() => result.current.approve())
  expect(client.approve).toHaveBeenCalledWith(revision, 'test-validation')
  expect(result.current.approval?.auditId).toBe('test-audit')
  act(() => result.current.changed({ ...result.current.draft, metadata: { ...result.current.draft.metadata, mapVersion: 'edited-v3' } }))
  expect(result.current.approval).toBeNull()
  expect(result.current.canApprove).toBe(false)
})

test('local errors and unverified flight tags never reach relay save or approval', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now))
  const draft = draftFixture(); draft.tags = [{ ...tagFixture(), tapeVerified: false }]
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  await act(() => result.current.approve())
  expect(client.save).not.toHaveBeenCalled()
  expect(client.approve).not.toHaveBeenCalled()
})

test('an edit during validation discards the old response and blocks approval', async () => {
  const client = clientFixture(), answer = deferred<MapValidation>()
  client.validate.mockImplementation(() => answer.promise)
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  let pending!: Promise<void>
  act(() => { pending = result.current.validate() })
  act(() => result.current.changed({ ...result.current.draft, metadata: { ...result.current.draft.metadata, floorId: 'other-floor' } }))
  await act(async () => { answer.resolve({ reference: revision, validationId: 'old', valid: true, issues: [] }); await pending })
  expect(result.current.validation).toBeNull()
  expect(result.current.canApprove).toBe(false)
  expect(result.current.notice).toContain('draft changed')
})

test('an approval returned after an edit cannot approve the new draft', async () => {
  const client = clientFixture(), answer = deferred<MapApproval>()
  client.approve.mockImplementation(() => answer.promise)
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  await act(() => result.current.validate())
  let pending!: Promise<void>
  act(() => { pending = result.current.approve() })
  act(() => result.current.changed({ ...result.current.draft, features: [] }))
  await act(async () => { answer.resolve({ reference: revision, validationId: 'test-validation', auditId: 'old-audit', approvedBy: 'Test', approvedAt: 1000 }); await pending })
  expect(result.current.approval).toBeNull()
})

test('concurrent-version save errors and mismatched validation identities fail closed', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  act(() => result.current.changed({ ...result.current.draft, metadata: { ...result.current.draft.metadata, mapVersion: 'v2' } }))
  client.save.mockRejectedValueOnce(new Error('Version conflict: reload the current relay revision.'))
  await act(() => result.current.save())
  expect(client.save).toHaveBeenLastCalledWith(expect.anything(), revision)
  expect(result.current.dirty).toBe(true)
  expect(result.current.notice).toContain('Version conflict')
  await act(() => result.current.load(revision))
  client.validate.mockResolvedValueOnce({ reference: nextRevision, validationId: 'wrong-revision', valid: true, issues: [] })
  await act(() => result.current.validate())
  expect(result.current.canApprove).toBe(false)
  expect(result.current.notice).toContain('exact saved revision')
})

test('client replacement discards pending work, clears visible busy state, and cannot rebind a saved document through list', async () => {
  const first = clientFixture(), second = clientFixture(), answer = deferred<MapValidation>()
  first.validate.mockImplementation(() => answer.promise)
  const { result, rerender } = renderHook(({ client }: { client: MapAuthoringClient }) => useMapAuthoring(client, now), { initialProps: { client: first } })
  await act(() => result.current.load(revision))
  let pending!: Promise<void>
  act(() => { pending = result.current.validate() })
  rerender({ client: second })
  expect(result.current.busy).toBeNull()
  expect(result.current.base).toBeNull()
  await act(() => result.current.list())
  expect(result.current.revisions).toHaveLength(2)
  expect(result.current.base).toBeNull()
  await act(async () => { answer.resolve({ reference: revision, validationId: 'old-client', valid: true, issues: [] }); await pending })
  expect(result.current.validation).toBeNull()
})

test('successful load, list, and comparison retain exact identities without stale-result notices', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.list())
  await act(() => result.current.load(revision))
  expect(result.current.notice).toContain('Relay revision loaded')
  await act(() => result.current.compare(nextRevision))
  expect(result.current.comparison?.right).toEqual(nextRevision)
})

test('a fresh associated observation updates the draft and clears tape claims', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now, rosterFixture()))
  const draft = draftFixture(); draft.tags = [tagFixture()]
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  await act(() => result.current.record('tag-record'))
  expect(result.current.draft.tags[0]).toMatchObject({ position: { x: 3, y: 3 }, tapeVerified: false, tapeEvidence: '', source: 'auto_registered' })
  expect(result.current.draft.tags[0].observations).toContain('new-observation')
  expect(result.current.notice).toContain('Fresh associated observation')
  expect(result.current.observationReference).toEqual(revision)
  expect(result.current.dirty).toBe(true)
})

test('unsaved matching map labels do not authorize recording or an observation overlay', async () => {
  const client = clientFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  const { result } = renderHook(() => useMapAuthoring(client, now, rosterFixture()))
  act(() => result.current.replace(draft))
  await act(() => result.current.record('tag-record'))
  expect(result.current.observationReference).toBeNull()
  expect(result.current.recordTarget).toBeNull()
  expect(client.recordCurrentObservation).not.toHaveBeenCalled()
})

test.each(['image', 'bytes', 'registration', 'resolution', 'origin'] as const)('changed %s retires recording and overlays despite unchanged map labels', async (field) => {
  const client = clientFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  const { result } = renderHook(() => useMapAuthoring(client, now, rosterFixture()))
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  expect(result.current.observationReference).toEqual(revision)
  const next = structuredClone(result.current.draft)
  if (field === 'image') next.image!.sha256 = 'c'.repeat(64)
  if (field === 'bytes') next.image!.dataUrl += 'AAAA'
  if (field === 'registration') next.metadata.registration!.transformId = 'different-transform'
  if (field === 'resolution') next.metadata.resolutionM = 0.5
  if (field === 'origin') next.metadata.originXM = 10
  act(() => result.current.changed(next))
  await act(() => result.current.record('tag-record'))
  expect(result.current.observationReference).toBeNull()
  expect(result.current.recordTarget).toBeNull()
  expect(client.recordCurrentObservation).not.toHaveBeenCalled()
})

test('a drive-over receipt for another bundle cannot reuse matching map and floor labels', async () => {
  const client = clientFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  const original = await client.recordCurrentObservation()
  client.recordCurrentObservation.mockResolvedValueOnce({ ...original, reference: { ...revision, bundleId: 'different-bundle' } })
  const { result } = renderHook(() => useMapAuthoring(client, now, rosterFixture()))
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  await act(() => result.current.record('tag-record'))
  expect(result.current.draft.tags[0].position).toEqual({ x: 2, y: 2 })
  expect(result.current.notice).toContain('fresh, verified')
})

test.each(['stale', 'unverified', 'wrong frame', 'wrong session', 'wrong epoch'] as const)('rejects %s observations without recording a position', async (mode) => {
  const client = clientFixture()
  const observation = await client.recordCurrentObservation()
  if (mode === 'stale') observation.tCapture = 100
  if (mode === 'unverified') observation.frameAssociationVerified = false
  if (mode === 'wrong frame') observation.mapVersion = 'unassociated-map'
  if (mode === 'wrong session') observation.sessionId = 'other'
  if (mode === 'wrong epoch') observation.connectionEpoch = 0
  client.recordCurrentObservation.mockResolvedValue(observation)
  const { result } = renderHook(() => useMapAuthoring(client, now, rosterFixture()))
  const draft = draftFixture(); draft.tags = [tagFixture()]
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  await act(() => result.current.record('tag-record'))
  expect(result.current.draft.tags[0].position).toEqual({ x: 2, y: 2 })
  expect(result.current.notice).toContain('fresh, verified')
})

test('unavailable runtime does not manufacture revisions, validation, or observations', async () => {
  const { result } = renderHook(() => useMapAuthoring(UNAVAILABLE_MAP_AUTHORING_CLIENT, now))
  await act(() => result.current.list())
  await act(() => result.current.save())
  await act(() => result.current.validate())
  await act(() => result.current.approve())
  expect(result.current.revisions).toEqual([])
  expect(result.current.base).toBeNull()
  expect(result.current.validation).toBeNull()
  expect(result.current.approval).toBeNull()
})

test('relay load rejects failed image-byte verification before binding a revision', async () => {
  const client = clientFixture()
  vi.mocked(verifyDraftImage).mockRejectedValueOnce(new Error('Image dimensions or SHA-256 do not match the actual raster.'))
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  expect(verifyDraftImage).toHaveBeenCalledWith(draftFixture())
  expect(result.current.base).toBeNull()
  expect(result.current.draft.image).toBeNull()
  expect(result.current.notice).toContain('actual raster')
  await act(() => result.current.validate())
  await act(() => result.current.approve())
  expect(client.validate).not.toHaveBeenCalled()
  expect(client.approve).not.toHaveBeenCalled()
})

test('image decode cannot rebind a load to a provider-mutated revision identity', async () => {
  const client = clientFixture(), response = deferred<MapDraft>(), returned = { ...revision }
  client.load.mockResolvedValueOnce({ reference: returned, draft: draftFixture() })
  vi.mocked(verifyDraftImage).mockImplementationOnce(() => response.promise)
  const { result } = renderHook(() => useMapAuthoring(client, now))
  let pending!: Promise<void>
  await act(async () => { pending = result.current.load(revision); await Promise.resolve() })
  returned.revision = 'mutated-during-decode'
  await act(async () => { response.resolve(draftFixture()); await pending })
  expect(result.current.base).toEqual(revision)
})

test.each(['tagId', 'family', 'sizeM', 'heightM', 'position', 'source'] as const)('editing tag %s invalidates the previous observation and tape claims', async (field) => {
  const client = clientFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  client.load.mockResolvedValueOnce({ reference: revision, draft })
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  await act(() => result.current.validate())
  const replacement = { tagId: 8, family: 'tag25h9', sizeM: 0.3, heightM: 1, position: { x: 4, y: 4 }, source: 'measured' }[field]
  act(() => result.current.changed({ ...result.current.draft, tags: [{ ...result.current.draft.tags[0], [field]: replacement }] }))
  expect(result.current.draft.tags[0]).toMatchObject({ tapeVerified: false, tapeEvidence: '', observations: [] })
  expect(result.current.validation).toBeNull()
  expect(result.current.canApprove).toBe(false)
  await act(() => result.current.save())
  expect(client.save).not.toHaveBeenCalled()
})

test('changing the map association retires tag and corridor measurement evidence, including after undo', () => {
  const draft = draftFixture(); draft.tags = [tagFixture()]
  draft.features.push({ ...draft.features[0], id: 'route', kind: 'corridor', name: 'Measured route', points: [{ x: 2, y: 2 }, { x: 8, y: 2 }], widthM: 1, flightHeightM: 1, heightToleranceM: 0.1, heightEvidence: 'Test tape survey' })
  const { result } = renderHook(() => useMapAuthoring(clientFixture(), now))
  act(() => result.current.replace(draft))
  act(() => result.current.changed({ ...result.current.draft, metadata: { ...draft.metadata, floorId: 'different-floor' } }))
  expect(result.current.draft.tags[0].tapeVerified).toBe(false)
  expect(result.current.draft.tags[0].observations).toEqual([])
  expect(result.current.draft.features[1].heightEvidence).toBe('')
  act(() => result.current.undo())
  expect(result.current.draft.metadata.floorId).toBe('test-floor')
  expect(result.current.draft.tags[0].tapeVerified).toBe(false)
  expect(result.current.draft.features[1].heightEvidence).toBe('')
})

test.each(['session', 'provider'] as const)('%s A→B→A permanently retires saved receipts and revision lists', async (mode) => {
  const first = clientFixture(), second = clientFixture()
  const { result, rerender } = renderHook(({ client }: { client: MapAuthoringClient }) => useMapAuthoring(client, now), { initialProps: { client: first } })
  await act(() => result.current.list())
  await act(() => result.current.load(revision))
  await act(() => result.current.validate())
  await act(() => result.current.approve())
  expect(result.current.approval).not.toBeNull()
  if (mode === 'session') first.sessionId = 'other-session'
  rerender({ client: mode === 'session' ? first : second })
  expect(result.current.approval).toBeNull()
  if (mode === 'session') first.sessionId = 'test-session'
  rerender({ client: first })
  expect(result.current.base).toBeNull()
  expect(result.current.validation).toBeNull()
  expect(result.current.approval).toBeNull()
  expect(result.current.revisions).toEqual([])
  expect(result.current.canApprove).toBe(false)
})

test('a pending response from a retired session cannot rebind after that same session returns', async () => {
  const client = clientFixture(), response = deferred<MapValidation>()
  client.validate.mockImplementation(() => response.promise)
  const { result, rerender } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  let pending!: Promise<void>
  act(() => { pending = result.current.validate() })
  client.sessionId = 'other-session'; rerender()
  expect(result.current.busy).toBeNull()
  client.sessionId = 'test-session'; rerender()
  await act(async () => { response.resolve({ reference: revision, validationId: 'retired', valid: true, issues: [] }); await pending })
  expect(result.current.base).toBeNull()
  expect(result.current.validation).toBeNull()
  expect(result.current.canApprove).toBe(false)
})

test('accepted provider receipts and comparison rows are detached from later provider mutation', async () => {
  const client = clientFixture()
  const receipt: MapValidation = { reference: { ...revision }, validationId: 'detached', valid: true, issues: [] }
  const approved: MapApproval = { reference: { ...revision }, validationId: 'detached', auditId: 'accepted-audit', approvedBy: 'Recorded operator', approvedAt: 1000 }
  const comparison: RevisionComparison = { left: { ...revision }, right: { ...nextRevision }, changes: [{ path: 'name', before: 'Before', after: 'After' }] }
  client.validate.mockResolvedValueOnce(receipt); client.approve.mockResolvedValueOnce(approved); client.compare.mockResolvedValueOnce(comparison)
  const { result, rerender } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  await act(() => result.current.validate())
  await act(() => result.current.approve())
  await act(() => result.current.compare(nextRevision))
  receipt.reference.revision = 'mutated'; receipt.valid = false; receipt.issues.push({ path: 'bad', message: 'Mutation' })
  approved.approvedBy = 'Mutation'; approved.reference.contentHash = 'f'.repeat(64)
  comparison.changes[0].after = 'Mutation'; comparison.left.revision = 'mutated'
  rerender()
  expect(result.current.validation).toMatchObject({ reference: revision, valid: true, issues: [] })
  expect(result.current.approval).toMatchObject({ reference: revision, approvedBy: 'Recorded operator' })
  expect(result.current.comparison).toMatchObject({ left: revision, changes: [{ after: 'After' }] })
  expect(result.current.canApprove).toBe(true)
})

test('malformed rendering evidence is refused instead of retained', async () => {
  const client = clientFixture()
  const { result } = renderHook(() => useMapAuthoring(client, now))
  await act(() => result.current.load(revision))
  client.validate.mockResolvedValueOnce({ reference: revision, validationId: 'bad', valid: true, issues: [{ path: {}, message: 'Invalid type' }] } as unknown as MapValidation)
  await act(() => result.current.validate())
  expect(result.current.validation).toBeNull()
  expect(result.current.canApprove).toBe(false)
  client.compare.mockResolvedValueOnce({ left: revision, right: nextRevision, changes: [{ path: 'name', before: 'Before', after: {} }] } as unknown as RevisionComparison)
  await act(() => result.current.compare(nextRevision))
  expect(result.current.comparison).toBeNull()
  expect(result.current.notice).toContain('malformed revision changes')
})

test('recording detaches a position and rechecks the current device epoch after asynchronous lookup', async () => {
  const client = clientFixture(), roster = rosterFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  const observation = await client.recordCurrentObservation()
  client.recordCurrentObservation.mockResolvedValueOnce(observation)
  const { result, rerender } = renderHook(({ state }) => useMapAuthoring(client, now, state), { initialProps: { state: roster } })
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  await act(() => result.current.record('tag-record'))
  observation.position.x = 99
  rerender({ state: roster })
  expect(result.current.draft.tags[0].position).toEqual({ x: 3, y: 3 })
  const response = deferred<CurrentTagObservation>()
  client.recordCurrentObservation.mockImplementationOnce(() => response.promise)
  let pending!: Promise<void>
  act(() => { pending = result.current.record('tag-record') })
  rerender({ state: { ...roster, aircraft: { 11: { ...roster.aircraft[11], connection_epoch: 3 } } } })
  await act(async () => { response.resolve({ ...observation, position: { x: 4, y: 4 } }); await pending })
  expect(result.current.draft.tags[0].position).toEqual({ x: 3, y: 3 })
  expect(result.current.notice).toContain('fresh, verified')
})

test('drive-over recording binds one selected ground robot and refuses another reported source', async () => {
  const client = clientFixture(), roster = rosterFixture(), draft = draftFixture()
  draft.tags = [tagFixture()]
  roster.aircraft[12] = { ...roster.aircraft[11], drone_id: 12, unit: 2 }
  const { result, rerender } = renderHook(({ state }) => useMapAuthoring(client, now, state), { initialProps: { state: roster } })
  act(() => result.current.replace(draft))
  await act(() => result.current.save())
  const observation = await client.recordCurrentObservation()
  client.recordCurrentObservation.mockClear()
  client.recordCurrentObservation.mockResolvedValueOnce({ ...observation, deviceId: 12 })
  await act(() => result.current.record('tag-record'))
  expect(client.recordCurrentObservation).toHaveBeenCalledWith({ reference: revision, mapVersion: draft.metadata.mapVersion,
    floorId: draft.metadata.floorId, tagId: 7, deviceId: 11, connectionEpoch: 2 })
  expect(result.current.draft.tags[0].position).toEqual({ x: 2, y: 2 })
  for (const selection of [[], [11, 12]]) {
    rerender({ state: { ...roster, selection } })
    client.recordCurrentObservation.mockClear()
    await act(() => result.current.record('tag-record'))
    expect(client.recordCurrentObservation).not.toHaveBeenCalled()
  }
})
