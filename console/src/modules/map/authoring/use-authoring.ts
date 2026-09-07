import { observeDevice } from '../../../control/observation'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ControlState } from '../../../control/state'
import { activationReceipt, approvalReceipt, revisionComparison, revisionIdentity, revisionList, sameRevision, supports, validRevision, validationReceipt, type AuthoringOperation, type MapAuthoringClient } from './client'
import { emptyDraft, invalidateChangedEvidence, validateDraft } from './geometry'
import { parseLocalDraft, verifyDraftImage } from './files'
import type { MapApproval, MapDraft, MapRevision, MapValidation, RevisionComparison, RevisionSummary } from './types'
import { coordinateImageIdentity, coordinateSourceIdentity, currentWorldObservation, observationClock } from './observations'

export function useMapAuthoring(client: MapAuthoringClient, now: () => number, state?: ControlState) {
  const session = client.status === 'available' ? client.sessionId : null
  // A→B→A gets a new ownership object, so old receipts cannot become current again.
  const binding = useMemo(() => ({ client, session }), [client, session])
  const [draft, setDraft] = useState(emptyDraft)
  const [history, setHistory] = useState<MapDraft[]>([])
  const [base, setBase] = useState<MapRevision | null>(null)
  const [savedCoordinateSource, setSavedCoordinateSource] = useState<string | null>(null)
  const [dirty, setDirty] = useState(true)
  const [validation, setValidation] = useState<MapValidation | null>(null)
  const [approval, setApproval] = useState<MapApproval | null>(null)
  const [receiptBinding, setReceiptBinding] = useState<typeof binding | null>(null)
  const [revisions, setRevisions] = useState<RevisionSummary[]>([])
  const [revisionBinding, setRevisionBinding] = useState<typeof binding | null>(null)
  const [comparison, setComparison] = useState<RevisionComparison | null>(null)
  const [notice, setNotice] = useState('Local draft. No relay approval.')
  const [pending, setPending] = useState<{ operation: AuthoringOperation; binding: typeof binding } | null>(null)
  const generation = useRef(0)
  const request = useRef(0)
  const inFlight = useRef(false)
  const latestRoster = useRef(state)
  useEffect(() => { latestRoster.current = state }, [state])
  useEffect(() => () => { request.current += 1; inFlight.current = false }, [client, session])
  const issues = validateDraft(draft)
  const bound = receiptBinding === binding
  const imageIdentity = useMemo(() => coordinateImageIdentity(draft.image), [draft.image])
  const coordinateSource = useMemo(() => coordinateSourceIdentity(draft.metadata, imageIdentity), [draft.metadata, imageIdentity])
  const observationReference = bound && base && savedCoordinateSource === coordinateSource ? base : null
  const currentValidation = bound && !dirty && validation && sameRevision(validation.reference, base) ? validation : null
  const currentApproval = bound && !dirty && approval && sameRevision(approval.reference, base) ? approval : null
  const busy = pending?.binding === binding ? pending.operation : null

  const changed = (next: MapDraft, retainHistory = true, recordedTag?: string) => {
    generation.current += 1
    if (retainHistory) setHistory((previous) => [...previous.slice(-49), draft])
    else setHistory([])
    setDraft(invalidateChangedEvidence(draft, next, recordedTag))
    setDirty(true)
    setValidation(null)
    setApproval(null)
    setComparison(null)
    setNotice('Local changes. Save and validate this exact revision before approval.')
  }
  const replace = (next: MapDraft) => {
    changed(next, false)
    // Imported evidence remains a local claim, independent of the previous draft.
    setDraft(next)
    setBase(null)
    setSavedCoordinateSource(null)
    setReceiptBinding(null)
  }
  const undo = () => {
    const previous = history.at(-1)
    if (!previous) return
    changed(previous, false)
    setHistory(history.slice(0, -1))
  }
  const run = async (operation: AuthoringOperation, work: (current: Extract<MapAuthoringClient, { status: 'available' }>, stillCurrent: () => boolean) => Promise<void>) => {
    if (client.status !== 'available' || !supports(client, operation) || inFlight.current) return
    const epoch = generation.current, id = ++request.current, sessionId = client.sessionId
    inFlight.current = true
    setPending({ operation, binding })
    let applied = false
    const stillCurrent = () => {
      applied = id === request.current && epoch === generation.current && client.sessionId === sessionId && supports(client, operation)
      return applied
    }
    try {
      await work(client, stillCurrent)
    } catch (error) {
      if (id === request.current) setNotice(error instanceof Error ? error.message : 'The relay operation failed.')
    } finally {
      if (id === request.current) {
        inFlight.current = false
        setPending(null)
        if (epoch !== generation.current && !applied) setNotice('The draft changed while the request was pending. Its result cannot validate or approve these changes.')
      }
    }
  }
  const list = () => run('list', async (api, current) => {
    const result = revisionList(await api.list())
    if (current()) { setRevisions(result); setRevisionBinding(binding); setNotice(result.length ? 'Relay revisions loaded.' : 'No saved relay revisions reported.') }
  })
  const load = (reference: MapRevision) => run('load', async (api, current) => {
    if (!validRevision(reference)) throw new Error('Select a valid relay revision identity.')
    const requested = revisionIdentity(reference)
    const result = await api.load(revisionIdentity(requested))
    if (!validRevision(result.reference) || !sameRevision(result.reference, requested)) throw new Error('The loaded revision does not match the requested identity.')
    const loaded = revisionIdentity(result.reference)
    const next = await verifyDraftImage(parseLocalDraft(JSON.stringify(result.draft)))
    if (current()) {
      replace(next)
      setBase(loaded); setDirty(false); setReceiptBinding(binding)
      setSavedCoordinateSource(coordinateSourceIdentity(next.metadata, coordinateImageIdentity(next.image)))
      setNotice('Relay revision loaded. Approval requires fresh server validation of this exact revision.')
    }
  })
  const save = () => run('save', async (api, current) => {
    if (issues.length) throw new Error('Resolve local checks before saving to the relay.')
    const result = await api.save(parseLocalDraft(JSON.stringify(draft)), bound && base ? revisionIdentity(base) : null)
    if (!validRevision(result)) throw new Error('The relay did not return an immutable saved revision.')
    if (bound && base && (result.bundleId !== base.bundleId || (dirty && sameRevision(result, base)))) throw new Error('The relay returned a conflicting or unchanged revision for edited content.')
    if (current()) {
      setBase(revisionIdentity(result)); setDirty(false); setReceiptBinding(binding); setValidation(null); setApproval(null)
      setSavedCoordinateSource(coordinateSource)
      setNotice('Saved to the relay. This revision is not yet validated or approved.')
    }
  })
  const validate = () => run('validate', async (api, current) => {
    if (!base || !bound || dirty || issues.length) throw new Error('Save a valid, unchanged draft before relay validation.')
    setValidation(null); setApproval(null)
    const result = validationReceipt(await api.validate(revisionIdentity(base)))
    if (!sameRevision(result.reference, base)) throw new Error('Validation did not match the exact saved revision.')
    if (current()) { setValidation(result); setApproval(null); setNotice(result.valid && result.issues.length === 0 ? 'Relay validation passed. Review and explicitly approve this revision.' : 'Relay validation refused this revision.') }
  })
  const canApprove = issues.length === 0 && currentValidation?.valid === true && currentValidation.issues.length === 0 && supports(client, 'approve')
  const approve = () => run('approve', async (api, current) => {
    if (!canApprove || !base || !currentValidation) throw new Error('Approval requires passing server validation of the unchanged saved revision.')
    const result = approvalReceipt(await api.approve(revisionIdentity(base), currentValidation.validationId))
    if (!sameRevision(result.reference, base) || result.validationId !== currentValidation.validationId || result.approvedAt < 0 || result.approvedAt > Math.min(now() + 1000, 8.64e15)) throw new Error('The relay did not return an audited approval for this exact validation and revision.')
    if (current()) { setApproval(result); setNotice('The relay returned audited approval for this exact revision.') }
  })
  const compare = (other: MapRevision) => run('compare', async (api, current) => {
    if (!base || !bound || dirty) throw new Error('Save current changes before comparing relay revisions.')
    if (!validRevision(other)) throw new Error('Select a valid relay revision identity.')
    const result = revisionComparison(await api.compare(revisionIdentity(base), revisionIdentity(other)))
    if (!sameRevision(result.left, base) || !sameRevision(result.right, other)) throw new Error('Comparison does not match the requested revisions.')
    if (current()) setComparison(result)
  })
  const activate = () => run('activate', async (api, current) => {
    if (!base || !bound || dirty || !api.selectForNavigation) throw new Error('Load or save an approved, unchanged revision before selecting the navigation map.')
    const result = activationReceipt(await api.selectForNavigation(revisionIdentity(base)))
    if (!sameRevision(result.reference, base)) throw new Error('The relay selected another map revision.')
    if (current()) setNotice(`Navigation map selected by ${result.selectedBy} · receipt ${result.selectionId}. No motion was requested.`)
  })
  const recordTarget = observationReference ? recordingTarget(state, session, now()) : null
  const record = (tagId: string) => run('record', async (api, current) => {
    if (!observationReference) throw new Error('Load or save this exact coordinate source before recording. Image and registration changes require a new approved source.')
    const reference = revisionIdentity(observationReference)
    const target = recordingTarget(latestRoster.current, api.sessionId, now())
    if (!target) throw new Error('Select one current ground robot to record its position.')
    const deviceId = target.drone_id, connectionEpoch = target.connection_epoch
    const tag = draft.tags.find((t) => t.id === tagId)
    if (!tag || tag.tagId === null || !Number.isInteger(tag.tagId) || tag.tagId < 0 || draft.metadata.frame !== 'world' || !draft.metadata.mapVersion.trim() || !draft.metadata.floorId.trim() || !latestRoster.current) throw new Error('Select a tag with an explicit ID, world-frame map, and current relay roster first.')
    const result = await api.recordCurrentObservation({ reference, mapVersion: draft.metadata.mapVersion, floorId: draft.metadata.floorId, tagId: tag.tagId, deviceId, connectionEpoch })
    const currentTarget = recordingTarget(latestRoster.current, api.sessionId, now())
    if (currentTarget?.drone_id !== deviceId || currentTarget.connection_epoch !== connectionEpoch ||
      result.deviceId !== deviceId || result.connectionEpoch !== connectionEpoch || result.tagId !== tag.tagId || !currentWorldObservation(result, draft.metadata, latestRoster.current, api.sessionId, now(), reference)) throw new Error('The observation lacks a fresh, verified map/frame/device association.')
    if (current()) {
      changed({ ...draft, tags: draft.tags.map((t) => t.id === tagId ? { ...t, position: { x: result.position.x, y: result.position.y }, source: 'auto_registered', confidence: result.confidence, observations: [...new Set([...t.observations, result.observationId])], tapeVerified: false, tapeEvidence: '' } : t) }, true, tagId)
      setNotice('Fresh associated observation recorded in the local draft. Tape verification must be repeated.')
    }
  })
  return { draft, changed, replace, undo, canUndo: history.length > 0, base: bound ? base : null, observationReference, dirty, issues, validation: currentValidation, approval: currentApproval, revisions: revisionBinding === binding ? revisions : [], comparison: bound ? comparison : null, busy, notice, setNotice, list, load, save, validate, approve, canApprove, compare, activate, record, recordTarget }
}

/** Drive-over recording names one physical ground robot, never an arbitrary fleet member. */
function recordingTarget(state: ControlState | undefined, session: string | null, at: number) {
  if (!state || state.sessionId !== session || state.selection.length !== 1 || !['connected', 'degraded'].includes(state.connection.status)) return null
  const device = state.aircraft[state.selection[0]]
  return device?.device_class === 'ground_vehicle' && observeDevice(device, observationClock(state, at)).state === 'current' ? device : null
}
