import { isNavigationPreview, type NavigationPreview } from '../navigation/client'
import type { IntentV1 } from '../relay/contract'

export interface SearchCatalog {
  target_classes: string[]
  zones: string[]
}

export interface SearchPreview extends NavigationPreview {
  preview: {
    zone_id: string
    target_class: string
    allocations: Array<{ drone_id: number; source_id: string; task_id: string; workload_cells: number; lane_count: number }>
  }
}

export interface SearchCell {
  cell_id: string
  x_m: number
  y_m: number
  z_m: number
  floor_id: string
}

export interface SearchTaskStatus {
  drone_id: number
  task_id: string
  state: string
  covered_cells: number
  total_cells: number
  covered_cell_ids: string[]
  cells: SearchCell[]
}

export interface SearchFinding {
  sighting_id: string
  source_id: string
  acknowledged: boolean
  label: string
  confidence: number
  bbox_xyxy: number[]
  observation_count: number
  frame: { frame_id: string; source_id: string; mission_id: string; worker_run_id: string | null; frame_sequence: number; decoded_at_monotonic_s: number; evaluated_at_monotonic_s: number } | null
  position: { x_m: number; y_m: number; z_m: number; zone_id: string; floor_id: string } | null
}

export interface SearchStatus {
  session: string
  intent_id: string
  state: string
  tasks: SearchTaskStatus[]
  candidates: SearchFinding[]
  detection_workers?: Array<{ drone_id: number; state: string; failure_reason?: string | null }>
}

export interface SearchClient {
  catalog(sessionId: string): Promise<SearchCatalog>
  preview(intent: IntentV1): Promise<SearchPreview>
  status(sessionId: string, intentId: string): Promise<SearchStatus>
  acknowledge(sessionId: string, intentId: string, sightingId: string): Promise<SearchStatus>
}

export class HttpSearchClient implements SearchClient {
  private readonly config: { baseUrl: string; token: string }
  private readonly fetcher: typeof fetch

  constructor(config: { baseUrl: string; token: string }, fetcher: typeof fetch = fetch) {
    this.config = config
    this.fetcher = fetcher
  }

  async catalog(sessionId: string): Promise<SearchCatalog> {
    const value = await this.request(sessionId, 'catalog')
    if (!record(value) || value.session !== sessionId || !stringArray(value.target_classes) || !stringArray(value.zones)) {
      throw new Error('The relay returned an invalid search catalog.')
    }
    return { target_classes: value.target_classes, zones: value.zones }
  }

  async preview(intent: IntentV1): Promise<SearchPreview> {
    const value = await this.request(intent.session, 'preview', { intent: { ...intent, confirm: true } })
    const args = intent.args as { zone_id: string; target_class: string }
    if (intent.name !== 'search' || !isNavigationPreview(value, intent) || !record(value) ||
      !record(value.preview) || value.preview.zone_id !== args.zone_id ||
      value.preview.target_class !== args.target_class || !Array.isArray(value.preview.allocations) ||
      !value.preview.allocations.every(allocation)) {
      throw new Error('The relay returned an invalid search preview.')
    }
    return value as unknown as SearchPreview
  }

  async status(sessionId: string, intentId: string): Promise<SearchStatus> {
    return this.parseStatus(await this.request(sessionId, encodeURIComponent(intentId)), sessionId)
  }

  async acknowledge(sessionId: string, intentId: string, sightingId: string): Promise<SearchStatus> {
    return this.parseStatus(await this.request(
      sessionId,
      `${encodeURIComponent(intentId)}/findings/${encodeURIComponent(sightingId)}/ack`,
      {},
    ), sessionId)
  }

  private parseStatus(value: unknown, sessionId: string): SearchStatus {
    if (!record(value) || value.session !== sessionId || !text(value.intent_id) || !text(value.state) || !Array.isArray(value.tasks) ||
      !value.tasks.every(task) || !Array.isArray(value.candidates) || !value.candidates.every(finding) ||
      (value.detection_workers !== undefined && (!Array.isArray(value.detection_workers) || !value.detection_workers.every(worker)))) {
      throw new Error('The relay returned an invalid search status.')
    }
    return value as unknown as SearchStatus
  }

  private async request(session: string, action: string, body?: object): Promise<unknown> {
    const url = new URL(this.config.baseUrl)
    url.protocol = url.protocol === 'wss:' || url.protocol === 'https:' ? 'https:' : 'http:'
    url.pathname = `/session/${encodeURIComponent(session)}/search/${action}`
    url.search = ''
    url.hash = ''
    const fetcher = this.fetcher
    const response = await fetcher(url.toString(), {
      method: body ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${this.config.token}`, 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
    })
    const value: unknown = await response.json()
    if (!response.ok) throw new Error(record(value) && text(value.detail) ? value.detail : 'Search is unavailable.')
    return value
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function text(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

function number(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(text)
}

function cell(value: unknown): boolean {
  return record(value) && text(value.cell_id) && text(value.floor_id) && number(value.x_m) && number(value.y_m) && number(value.z_m)
}

function task(value: unknown): boolean {
  return record(value) && Number.isInteger(value.drone_id) && text(value.task_id) && text(value.state) &&
    Number.isInteger(value.covered_cells) && Number.isInteger(value.total_cells) && stringArray(value.covered_cell_ids) &&
    Array.isArray(value.cells) && value.cells.every(cell)
}

function allocation(value: unknown): boolean {
  return record(value) && Number.isInteger(value.drone_id) && text(value.source_id) && text(value.task_id) &&
    Number.isInteger(value.workload_cells) && Number.isInteger(value.lane_count)
}

function finding(value: unknown): boolean {
  if (!record(value) || !text(value.sighting_id) || !text(value.source_id) || typeof value.acknowledged !== 'boolean' ||
    !text(value.label) || !number(value.confidence) || !Array.isArray(value.bbox_xyxy) || !value.bbox_xyxy.every(number) ||
    !Number.isInteger(value.observation_count)) return false
  if (value.position !== null && (!record(value.position) || !number(value.position.x_m) || !number(value.position.y_m) ||
    !number(value.position.z_m) || !text(value.position.zone_id) || !text(value.position.floor_id))) return false
  return value.frame === null || (record(value.frame) && text(value.frame.frame_id) && text(value.frame.source_id) &&
    text(value.frame.mission_id) && (typeof value.frame.worker_run_id === 'string' || value.frame.worker_run_id === null) &&
    Number.isInteger(value.frame.frame_sequence) && number(value.frame.decoded_at_monotonic_s) && number(value.frame.evaluated_at_monotonic_s))
}

function worker(value: unknown): boolean {
  return record(value) && Number.isInteger(value.drone_id) && text(value.state) &&
    (value.failure_reason === undefined || value.failure_reason === null || text(value.failure_reason))
}
