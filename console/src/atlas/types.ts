export type SpaceCategory = 'incident' | 'hazard' | 'community' | 'survey'
export interface GeoPosition {
  latitude: number
  longitude: number
  accuracy: number
  timestamp: number
  altitude?: number | null
  heading?: number | null
}
export interface NewSpace {
  title: string
  description: string
  category: SpaceCategory
  latitude: number
  longitude: number
  radius: number
  place: string
}
export interface Space extends NewSpace {
  id: string
  created_at: number
  updated_at: number
  status: 'active' | 'resolved'
  verification: 'unverified'
  capture_count: number
  coverage_percent: number
  contributors: number
  /** Actionable location/current-build requests, not a surface-completeness estimate. */
  open_request_count?: number
}
export type CaptureResponseTarget =
  | { kind: 'location'; cell_id: string }
  | { kind: 'surface'; job_id: string; artifact_sha256: string; region_id: string }
export interface CaptureRequestContext {
  target: CaptureResponseTarget
  label: string
  note: string
}
export interface CaptureMetadata {
  contributor_id: string
  name: string
  kind: 'photo' | 'video' | 'panorama'
  source: 'camera' | 'import'
  /** Null for imports when the original capture time has not been established. */
  captured_at: number | null
  position: GeoPosition | null
  note: string
  response_to?: CaptureResponseTarget
}
export interface Capture extends CaptureMetadata {
  id: string
  uploaded_at: number
  mime: string
  bytes: number
  sha256: string
}
export interface CoverageCell {
  id: string
  x: number
  y: number
  size: number
  captures: number
  latitude: number
  longitude: number
}
export interface SpaceRequest {
  capture_ids?: string[]
  cell_id: string
  note: string
  created_at: number
  latitude: number
  longitude: number
  status: 'open' | 'captured'
}
export interface Reconstruction {
  id?: string
  status: string
  source_count: number
  detail: string
  progress?: number
  new_source_count?: number
  registered_views?: number
  prepared_views?: number
  registered_captures?: number
  points?: number
  mean_reprojection_error_px?: number
  artifact_sha256?: string
  artifact_bytes?: number
  components?: number
  representation?: 'sparse_point_cloud' | 'textured_mesh'
  faces?: number
  vertices?: number
  dense_points?: number
  experimental?: boolean
  surface_review?: SurfaceReview | null
}
export interface SurfaceRegionSummary {
  id: string
  label: string
  center: [number, number, number]
  radius: number
  boundary_edges: number
}
export interface SurfaceRegion extends SurfaceRegionSummary {
  segments: [number, number, number][]
}
export interface SurfaceReview {
  method: string
  boundary_edges: number
  nonmanifold_edges: number
  candidate_regions?: number
  regions: SurfaceRegionSummary[]
}
export interface SurfaceFocus {
  job_id: string
  artifact_sha256: string
  region: SurfaceRegion
}
export interface SurfaceRequest {
  capture_ids?: string[]
  job_id: string
  artifact_sha256: string
  region_id: string
  label: string
  note: string
  status: 'open' | 'dismissed'
  created_at: number
  updated_at: number
}
export interface SpaceDetail {
  space: Space
  captures: Capture[]
  coverage: {
    cells: CoverageCell[]
    observed: number
    total: number
    percent: number
    qualified_captures: number
    meaning: string
  }
  people: {
    contributor_id: string
    name: string
    position: GeoPosition
    updated_at: number
  }[]
  requests: SpaceRequest[]
  reconstruction: Reconstruction
  surface_requests?: SurfaceRequest[]
}
export const CATEGORY_LABEL: Record<SpaceCategory, string> = {
  incident: 'Incident report',
  hazard: 'Local hazard',
  community: 'Community',
  survey: 'Area survey',
}
