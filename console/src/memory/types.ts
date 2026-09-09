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
  status: 'running' | 'complete' | 'partial' | 'failed' | 'interrupted' | 'outdated'
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
  audio?: { analyzed_seconds: number; rms_dbfs: number | null; note: string } | null
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
  revision: number
  capture: Capture
  notes: MemoryNotes
  assets: MemoryAsset[]
  inspection: Inspection | null
  analysis: MemoryAnalysis | null
  can_edit: boolean
  capabilities: { metadata: boolean; ai: boolean; weather: boolean; media_tools: boolean }
}
export const EMPTY_NOTES: MemoryNotes = {
  description: '',
  feeling: '',
  occurred_at: null,
  location: null,
  music_title: '',
  music_url: '',
}
