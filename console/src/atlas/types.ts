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
}
export interface CaptureMetadata {
  contributor_id: string
  name: string
  kind: 'photo' | 'video' | 'panorama'
  source: 'camera' | 'import'
  captured_at: number
  position: GeoPosition | null
  note: string
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
}
export const CATEGORY_LABEL: Record<SpaceCategory, string> = {
  incident: 'Incident report',
  hazard: 'Local hazard',
  community: 'Community',
  survey: 'Area survey',
}
