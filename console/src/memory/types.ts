import type { Capture } from '../atlas/types'

export interface MemoryNotes {
  description: string
  feeling: string
  occurred_at: string | null
  location: { latitude: number; longitude: number } | null
  music_title: string
  music_url: string
}
export interface MemoryAsset {
  id: string
  title: string
  role: 'ambient' | 'narration' | 'soundtrack'
  mime: string
  bytes: number
  sha256: string
  added_at: number
  rights_confirmed: true
  added_by?: string
}
export interface Inspection {
  mime: string
  width?: number
  height?: number
  has_audio: boolean
  duration_seconds?: number
  timestamp: string | null
  local_timestamp: string | null
  location: MemoryNotes['location']
  warnings: string[]
}
export interface MemoryAnalysis {
  id: string
  request_id?: string | null
  status: 'running' | 'complete' | 'partial' | 'failed' | 'interrupted' | 'outdated' | 'cancelling' | 'cancelled'
  started_at: number
  finished_at?: number
  warnings?: string[]
  inspection?: Inspection
  weather?: {
    provider: string
    source_url: string
    attribution: string
    dataset: string
    note: string
    sampled_at: string
    fields: Record<string, { value: number; unit: string }>
  } | null
  audio?: {
    analyzed_seconds: number
    rms_dbfs: number | null
    note: string
  } | null
  transcript?: { text: string; model: string; kind: string } | null
  suggestion?: {
    model: string
    summary: string
    visual_observations: string[]
    atmosphere_suggestions: string[]
    uncertainties: string[]
  } | null
}
export interface MemoryContext {
  review?: {
    revision: number
    analysis_id: string | null
    reviewed_at: number
    actor?: string
  } | null
  last_edit?: {
    actor: string
    changed_at: number
    kind: string
    sequence: number
  }
  revision: number
  capture: Capture
  notes: MemoryNotes
  assets: MemoryAsset[]
  inspection: Inspection | null
  analysis: MemoryAnalysis | null
  can_edit: boolean
  can_analyze?: boolean
  can_remove?: boolean
  can_cancel?: boolean
  analysis_idempotency?: boolean
  analysis_allowance?: {
    unit: 'provider_stage_reservation'
    limits: { space: number; relay: number }
    reserved: { space: number; relay: number }
    remaining: number
    resets_at: number
    error: string | null
  }
  analysis_request?: {
    id: string | null
    analysis_id: string | null
    rejection: string | null
    replayed: boolean
    current: boolean
  }
  capabilities: {
    metadata: boolean
    ai: boolean
    weather: boolean
    media_tools: boolean
  }
}
export interface MemoryAnalysisRequest {
  revision: number
  weather: boolean
  ai: boolean
  audio_asset_id: string | null
  request_id?: string
}
export interface MemoryEdit {
  sequence: number
  revision: number
  actor: string
  changed_at: number
  kind: 'notes' | 'recording' | 'review'
  previous?: MemoryNotes
  notes?: MemoryNotes
  asset?: MemoryAsset
  review?: MemoryContext['review']
}
export function analysisActive(analysis?: MemoryAnalysis | null): boolean {
  return !!analysis && ['running', 'interrupted', 'cancelling'].includes(analysis.status)
}
export function memoryActor(actor?: string): string {
  return !actor
    ? 'Earlier editor (not recorded)'
    : actor === 'workspace-operator'
      ? 'Workspace operator'
      : `Account ${actor.replace(/^acct_/, '').slice(0, 8)}`
}
export const EMPTY_NOTES: MemoryNotes = {
  description: '',
  feeling: '',
  occurred_at: null,
  location: null,
  music_title: '',
  music_url: '',
}
