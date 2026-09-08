/** Editor document stored by the relay; approval publishes a validated world bundle. */
export interface MapDraft {
  format: 'sweep-map-draft-v1'
  metadata: {
    mapVersion: string
    floorId: string
    frame: string
    resolutionM: number | null
    originXM: number | null
    originYM: number | null
    units?: string
    createdAt?: number | null
    creationEvidence?: string
    registration?: {
      sourceFrame: string
      transformId: string
      residualM: number | null
      thresholdM: number | null
      evidence: string
    }
  }
  image: OccupancyImage | null
  features: MapFeature[]
  tags: MapTag[]
}

export interface OccupancyImage {
  name: string
  dataUrl: string
  width: number
  height: number
  sha256: string
}

export interface XY { x: number; y: number }
export type FeatureKind = 'zone' | 'geofence' | 'no_fly' | 'obstacle' | 'corridor'
export interface MapFeature {
  id: string
  kind: FeatureKind
  name: string
  aliases: string[]
  /** Polygons include their closing point; corridors are open centerlines. */
  points: XY[]
  widthM: number | null
  flightHeightM: number | null
  heightToleranceM: number | null
  heightEvidence: string
}

export type TagSource = 'unreported' | 'measured' | 'surveyed' | 'auto_registered'
export interface MapTag {
  id: string
  tagId: number | null
  family: string
  sizeM: number | null
  position: XY
  heightM: number | null
  yawRad?: number | null
  source: TagSource
  confidence: number | null
  observations: string[]
  usedForFlight: boolean
  tapeVerified: boolean
  tapeEvidence: string
}

export interface DraftIssue { path: string; message: string }
export interface MapRevision { bundleId: string; revision: string; contentHash: string }
export interface RevisionSummary extends MapRevision { label: string }
export interface SavedMap { reference: MapRevision; draft: MapDraft }
export interface MapValidation {
  reference: MapRevision
  validationId: string
  valid: boolean
  issues: DraftIssue[]
}
export interface MapApproval {
  reference: MapRevision
  validationId: string
  auditId: string
  approvedBy: string
  approvedAt: number
}
export interface MapActivation {
  reference: MapRevision
  selectionId: string
  selectedBy: string
  selectedAt: number
}
export interface RevisionComparison {
  left: MapRevision
  right: MapRevision
  changes: Array<{ path: string; before: string; after: string }>
}
export interface WorldPositionObservation {
  reference: MapRevision
  observationId: string
  sourceId: string
  deviceId: number
  connectionEpoch: number
  sessionId: string
  frame: 'world'
  mapVersion: string
  floorId: string
  position: XY
  tCapture: number
  tIngest: number
  confidence: number
  /** Must come from an authoritative frame/epoch association, never legacy x/y. */
  frameAssociationVerified: boolean
}
export interface CurrentTagObservation extends WorldPositionObservation { tagId: number }
