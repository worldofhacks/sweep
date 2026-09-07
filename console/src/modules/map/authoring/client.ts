import type {
  CurrentTagObservation, MapApproval, MapDraft, MapRevision, MapValidation,
  RevisionComparison, RevisionSummary, SavedMap,
} from './types'

export type AuthoringOperation = 'list' | 'load' | 'save' | 'validate' | 'approve' | 'compare' | 'record'

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
    recordCurrentObservation: (request: { mapVersion: string; floorId: string; tagId: number }) => Promise<CurrentTagObservation>
  }

export const UNAVAILABLE_MAP_AUTHORING_CLIENT: MapAuthoringClient = {
  status: 'unavailable',
  reason: 'Relay map authoring is unavailable. Local drafts are not saved, validated, versioned, or approved by the relay.',
}

export function supports(client: MapAuthoringClient, operation: AuthoringOperation): boolean {
  return client.status === 'available' && Boolean(client.sessionId.trim()) && client.capabilities.includes(operation)
}

export function sameRevision(a: MapRevision | null, b: MapRevision | null): boolean {
  return a !== null && b !== null && a.bundleId === b.bundleId && a.revision === b.revision && a.contentHash === b.contentHash
}

export function validRevision(value: MapRevision): boolean {
  return typeof value?.bundleId === 'string' && value.bundleId.trim().length > 0
    && typeof value.revision === 'string' && value.revision.trim().length > 0
    && /^[a-f0-9]{64}$/.test(value.contentHash)
}
