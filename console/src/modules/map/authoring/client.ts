import type {
  CurrentTagObservation, MapApproval, MapDraft, MapRevision, MapValidation,
  RevisionComparison, RevisionSummary, SavedMap, WorldPositionObservation,
} from './types'

export type AuthoringOperation = 'list' | 'load' | 'save' | 'validate' | 'approve' | 'compare' | 'record' | 'observe'

/** Host-supplied adapter port. No HTTP routes or backend schema are invented here. */
export type MapAuthoringClient =
  | { status: 'unavailable'; reason: string }
  | {
    status: 'available'
    sessionId: string
    capabilities: readonly AuthoringOperation[]
    list: () => Promise<RevisionSummary[]>
    load: (reference: MapRevision) => Promise<SavedMap>
    save: (draft: MapDraft, expectedRevision: MapRevision | null) => Promise<MapRevision>
    validate: (reference: MapRevision) => Promise<MapValidation>
    approve: (reference: MapRevision, validationId: string) => Promise<MapApproval>
    compare: (left: MapRevision, right: MapRevision) => Promise<RevisionComparison>
    recordCurrentObservation: (request: { mapVersion: string; floorId: string; tagId: number; deviceId: number; connectionEpoch: number }) => Promise<CurrentTagObservation>
    /** Optional, explicitly verified observations; never adapted from generic telemetry. */
    subscribePositions?: (request: { mapVersion: string; floorId: string }, onObservation: (observation: WorldPositionObservation) => void, onError: (detail: string) => void) => () => void
  }

export const UNAVAILABLE_MAP_AUTHORING_CLIENT: MapAuthoringClient = {
  status: 'unavailable',
  reason: 'Relay map authoring is unavailable. Local drafts are not saved, validated, versioned, or approved by the relay.',
}

export function supports(client: MapAuthoringClient, operation: AuthoringOperation): boolean {
  return client.status === 'available' && Boolean(client.sessionId.trim()) && client.capabilities.includes(operation)
}

export function sameRevision(a: MapRevision | null, b: MapRevision | null): boolean {
  return a != null && b != null && a.bundleId === b.bundleId && a.revision === b.revision && a.contentHash === b.contentHash
}

export function revisionIdentity(value: MapRevision): MapRevision {
  return { bundleId: value.bundleId, revision: value.revision, contentHash: value.contentHash }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function text(value: unknown, max: number, empty = false): value is string {
  return typeof value === 'string' && value.length <= max && (empty || value.trim().length > 0)
}

export function validRevision(value: unknown): value is MapRevision {
  return record(value) && text(value.bundleId, 256) && text(value.revision, 256)
    && typeof value.contentHash === 'string' && /^[a-f0-9]{64}$/.test(value.contentHash)
}

/** Copy provider responses before retaining them; provider-owned objects are mutable. */
export function revisionList(value: unknown): RevisionSummary[] {
  if (!Array.isArray(value) || value.length > 256) throw new Error('The relay returned invalid revision identities.')
  const seen = new Set<string>()
  return value.map((entry: unknown) => {
    if (!validRevision(entry) || !('label' in entry) || !text(entry.label, 256)) throw new Error('The relay returned invalid revision identities.')
    const identity = revisionIdentity(entry), key = JSON.stringify(identity)
    if (seen.has(key)) throw new Error('The relay returned duplicate revision identities.')
    seen.add(key)
    return { ...identity, label: entry.label }
  })
}

export function validationReceipt(value: unknown): MapValidation {
  if (!record(value) || !validRevision(value.reference) || !text(value.validationId, 256)
    || typeof value.valid !== 'boolean' || !Array.isArray(value.issues) || value.issues.length > 256) throw new Error('The relay returned malformed validation evidence.')
  const issues = value.issues.map((issue: unknown) => {
    if (!record(issue) || !text(issue.path, 512) || !text(issue.message, 2048)) throw new Error('The relay returned malformed validation issues.')
    return { path: issue.path, message: issue.message }
  })
  return { reference: revisionIdentity(value.reference), validationId: value.validationId, valid: value.valid, issues }
}

export function approvalReceipt(value: unknown): MapApproval {
  if (!record(value) || !validRevision(value.reference) || !text(value.validationId, 256) || !text(value.auditId, 256)
    || !text(value.approvedBy, 256) || typeof value.approvedAt !== 'number' || !Number.isSafeInteger(value.approvedAt)) throw new Error('The relay returned malformed approval evidence.')
  return { reference: revisionIdentity(value.reference), validationId: value.validationId, auditId: value.auditId, approvedBy: value.approvedBy, approvedAt: value.approvedAt }
}

export function revisionComparison(value: unknown): RevisionComparison {
  if (!record(value) || !validRevision(value.left) || !validRevision(value.right)
    || !Array.isArray(value.changes) || value.changes.length > 1024) throw new Error('The relay returned malformed revision changes.')
  const changes = value.changes.map((change: unknown) => {
    if (!record(change) || !text(change.path, 512) || !text(change.before, 4096, true) || !text(change.after, 4096, true)) throw new Error('The relay returned malformed revision changes.')
    return { path: change.path, before: change.before, after: change.after }
  })
  return { left: revisionIdentity(value.left), right: revisionIdentity(value.right), changes }
}
