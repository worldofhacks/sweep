import { useEffect, useRef, useState } from 'react'
import { sameRevision, supports, validRevision, type AuthoringOperation, type MapAuthoringClient } from './client'
import { emptyDraft, validateDraft } from './geometry'
import { parseLocalDraft } from './files'
import type { MapApproval, MapDraft, MapRevision, MapValidation, RevisionComparison, RevisionSummary } from './types'

export function useMapAuthoring(client: MapAuthoringClient, now: () => number) {
  const [draft, setDraft] = useState(emptyDraft)
  const [history, setHistory] = useState<MapDraft[]>([])
  const [base, setBase] = useState<MapRevision | null>(null)
  const [dirty, setDirty] = useState(true)
  const [validation, setValidation] = useState<MapValidation | null>(null)
  const [approval, setApproval] = useState<MapApproval | null>(null)
  const [receiptClient, setReceiptClient] = useState<MapAuthoringClient | null>(null)
  const [revisions, setRevisions] = useState<RevisionSummary[]>([])
  const [comparison, setComparison] = useState<RevisionComparison | null>(null)
  const [notice, setNotice] = useState('Local draft. No relay approval.')
  const [busy, setBusy] = useState<AuthoringOperation | null>(null)
  const generation = useRef(0)
  const request = useRef(0)
  const inFlight = useRef(false)
  useEffect(() => () => { request.current += 1; inFlight.current = false }, [client])
  const issues = validateDraft(draft)
  const bound = receiptClient === client
  const currentValidation = bound && !dirty && validation && sameRevision(validation.reference, base) ? validation : null
  const currentApproval = bound && !dirty && approval && sameRevision(approval.reference, base) ? approval : null

  const changed = (next: MapDraft, retainHistory = true) => {
    generation.current += 1
    if (retainHistory) setHistory((previous) => [...previous.slice(-49), draft])
    else setHistory([])
    setDraft(next)
    setDirty(true)
    setValidation(null)
    setApproval(null)
    setComparison(null)
    setNotice('Local changes. Save and validate this exact revision before approval.')
  }
  const replace = (next: MapDraft) => {
    changed(next, false)
    setBase(null)
    setReceiptClient(null)
  }
  const undo = () => {
    const previous = history.at(-1)
    if (!previous) return
    changed(previous, false)
    setHistory(history.slice(0, -1))
  }
  const run = async (operation: AuthoringOperation, work: (current: Extract<MapAuthoringClient, { status: 'available' }>, stillCurrent: () => boolean) => Promise<void>) => {
    if (client.status !== 'available' || !supports(client, operation) || inFlight.current) return
    const epoch = generation.current, id = ++request.current
    inFlight.current = true
    setBusy(operation)
    const stillCurrent = () => id === request.current && epoch === generation.current
    try {
      await work(client, stillCurrent)
    } catch (error) {
      if (id === request.current) setNotice(error instanceof Error ? error.message : 'The relay operation failed.')
    } finally {
      if (id === request.current) {
        inFlight.current = false
        setBusy(null)
        if (epoch !== generation.current) setNotice('The draft changed while the request was pending. Its result cannot validate or approve these changes.')
      }
    }
  }
  const list = () => run('list', async (api, current) => {
    const result = await api.list()
    if (result.length > 256 || result.some((r) => !validRevision(r))) throw new Error('The relay returned invalid revision identities.')
    if (current()) { setRevisions(result); setReceiptClient(api); setNotice(result.length ? 'Relay revisions loaded.' : 'No saved relay revisions reported.') }
  })
  const load = (reference: MapRevision) => run('load', async (api, current) => {
    const result = await api.load(reference)
    if (!sameRevision(result.reference, reference) || !validRevision(result.reference)) throw new Error('The loaded revision does not match the requested identity.')
    const next = parseLocalDraft(JSON.stringify(result.draft))
    if (current()) {
      replace(next)
      setBase(result.reference); setDirty(false); setReceiptClient(api)
      setNotice('Relay revision loaded. Approval requires fresh server validation of this exact revision.')
    }
  })
  const save = () => run('save', async (api, current) => {
    if (issues.length) throw new Error('Resolve local checks before saving to the relay.')
    const result = await api.save(draft, bound ? base : null)
    if (!validRevision(result)) throw new Error('The relay did not return an immutable saved revision.')
    if (bound && base && (result.bundleId !== base.bundleId || (dirty && sameRevision(result, base)))) throw new Error('The relay returned a conflicting or unchanged revision for edited content.')
    if (current()) {
      setBase(result); setDirty(false); setReceiptClient(api); setValidation(null); setApproval(null)
      setNotice('Saved to the relay. This revision is not yet validated or approved.')
    }
  })
  const validate = () => run('validate', async (api, current) => {
    if (!base || !bound || dirty || issues.length) throw new Error('Save a valid, unchanged draft before relay validation.')
    const result = await api.validate(base)
    if (!sameRevision(result.reference, base) || !result.validationId?.trim() || typeof result.valid !== 'boolean' || !Array.isArray(result.issues)) throw new Error('Validation did not match the exact saved revision.')
    if (current()) { setValidation(result); setApproval(null); setNotice(result.valid && result.issues.length === 0 ? 'Relay validation passed. Review and explicitly approve this revision.' : 'Relay validation refused this revision.') }
  })
  const canApprove = issues.length === 0 && currentValidation?.valid === true && currentValidation.issues.length === 0 && supports(client, 'approve')
  const approve = () => run('approve', async (api, current) => {
    if (!canApprove || !base || !currentValidation) throw new Error('Approval requires passing server validation of the unchanged saved revision.')
    const result = await api.approve(base, currentValidation.validationId)
    if (!sameRevision(result.reference, base) || result.validationId !== currentValidation.validationId || !result.auditId?.trim() || !result.approvedBy?.trim() || !Number.isFinite(result.approvedAt)) throw new Error('The relay did not return an audited approval for this exact validation and revision.')
    if (current()) { setApproval(result); setNotice('The relay returned audited approval for this exact revision.') }
  })
  const compare = (other: MapRevision) => run('compare', async (api, current) => {
    if (!base || !bound || dirty) throw new Error('Save current changes before comparing relay revisions.')
    const result = await api.compare(base, other)
    if (!sameRevision(result.left, base) || !sameRevision(result.right, other)) throw new Error('Comparison does not match the requested revisions.')
    if (current()) setComparison(result)
  })
  const record = (tagId: string) => run('record', async (api, current) => {
    const tag = draft.tags.find((t) => t.id === tagId)
    if (!tag || tag.tagId === null || draft.metadata.frame !== 'world') throw new Error('Select a tag with an explicit ID and world-frame map first.')
    const result = await api.recordCurrentObservation({ mapVersion: draft.metadata.mapVersion, floorId: draft.metadata.floorId, tagId: tag.tagId })
    const at = now()
    if (result.frameAssociationVerified !== true || result.frame !== 'world' || result.mapVersion !== draft.metadata.mapVersion || result.floorId !== draft.metadata.floorId || result.tagId !== tag.tagId || result.sessionId !== api.sessionId
      || !Number.isInteger(result.deviceId) || result.deviceId < 1 || !Number.isInteger(result.connectionEpoch) || result.connectionEpoch < 1
      || !result.observationId?.trim() || !result.sourceId?.trim() || !Number.isFinite(result.tCapture) || !Number.isFinite(result.tIngest)
      || result.tCapture > result.tIngest || result.tIngest > at || at - result.tCapture > 1000
      || !Number.isFinite(result.confidence) || result.confidence < 0 || result.confidence > 1 || !Number.isFinite(result.position.x) || !Number.isFinite(result.position.y)) throw new Error('The observation lacks a fresh, verified map/frame/device association.')
    if (current()) changed({ ...draft, tags: draft.tags.map((t) => t.id === tagId ? { ...t, position: result.position, source: 'auto_registered', confidence: result.confidence, observations: [...new Set([...t.observations, result.observationId])], tapeVerified: false, tapeEvidence: '' } : t) })
  })
  return { draft, changed, replace, undo, canUndo: history.length > 0, base: bound ? base : null, dirty, issues, validation: currentValidation, approval: currentApproval, revisions: bound ? revisions : [], comparison: bound ? comparison : null, busy, notice, setNotice, list, load, save, validate, approve, canApprove, compare, record }
}
