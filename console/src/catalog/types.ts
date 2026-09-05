import type { CapturePattern, DroneId } from '../relay/contract'

/**
 * Catalog records for the Captures, Worlds, Connectivity, and Configuration
 * modules. The relay exposes no endpoint for any of these yet, so a field the
 * relay would own is `null` when unreported and the modules say so. Field
 * names follow the relay contract's snake_case so a future endpoint slots in.
 */

export type CoverageLabel = 'full_equirectangular' | 'incomplete_vertical_coverage'
export type CaptureQuality = 'pass' | 'fail'

export interface CapturePose {
  x: number
  y: number
  z: number
  yaw_deg: number
  gimbal_pitch_deg: number
  focal_mm: number
}

export interface CaptureRecord {
  capture_id: string
  project: string
  room_id: string
  drone_id: DroneId
  pattern: CapturePattern
  coverage: CoverageLabel
  files: number
  captured_at: number
  quality: CaptureQuality
  needs_retake: boolean
  checksum: string | null
  pose: CapturePose | null
}

export type RoomCaptureStatus = 'captured' | 'capturing' | 'needs_retake' | 'not_captured'

/** A drone bundle carries its capture id; the manual phone fallback carries none. */
export type BundleKind = CapturePattern | 'manual_phone'

export interface BundleRef {
  kind: BundleKind
  capture_id: string | null
}

export interface RoomRecord {
  room_id: string
  capture_status: RoomCaptureStatus
  /** Rooms reachable through a doorway, by room id. */
  doorways: string[]
  accepted_bundle: BundleRef | null
  /** Phone photos added on the Worlds page for the manual fallback. */
  manual_photos: number
  model: string
}

export interface Building {
  building_id: string
  label: string
  /** Reference only, never a generation input. */
  floor_plan: string | null
  rooms: RoomRecord[]
}

export type GenerationJobState =
  | 'draft'
  | 'uploading'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timed_out'

export interface GenerationJob {
  room_id: string
  state: GenerationJobState
  operation_id: string | null
  world_id: string | null
  model: string
  updated_at: number
  assets: string
  public: false
  /** The bundle the job was submitted with; a retry reuses it unchanged. */
  bundle: BundleRef | null
}

export interface NodeRecord {
  drone_id: DroneId
  rc_firmware: string | null
  aircraft_firmware: string | null
  phone_model: string | null
  sdk_release: string | null
  rtt_ms: number | null
  telemetry_rate_hz: number | null
  storage_free_gb: number | null
}

export type ServiceTone = 'ok' | 'warn' | 'danger'

export interface ServiceRecord {
  service_id: string
  label: string
  status: string
  tone: ServiceTone
  note: string
}

export interface HealthMetric {
  key: string
  value: string
  note: string
  tone: 'ok' | 'warn' | 'ink'
}

export interface ConfigField {
  key: string
  label: string
  value: string
}

export interface ConfigGroup {
  group_id: string
  title: string
  /** Safety-sensitive groups are staged and applied between runs. */
  staged: boolean
  fields: ConfigField[]
}

export interface StagedChange {
  group_id: string
  key: string
  value: string
}

export interface ModeRecord {
  mode: string
  positioning: string
  box: string
  spacing: string
  speed: string
  note: string
  status: 'accepted' | 'unsupported'
}

export interface ConfigSnapshot {
  groups: ConfigGroup[]
  staged_changes: StagedChange[]
  modes: ModeRecord[]
}

export interface CatalogSnapshot {
  captures: CaptureRecord[] | null
  building: Building | null
  jobs: GenerationJob[] | null
  nodes: Record<DroneId, NodeRecord> | null
  services: ServiceRecord[] | null
  metrics: HealthMetric[] | null
  config: ConfigSnapshot | null
}
