import type { IntentV1, SearchArgs } from '../relay/contract'

export interface SearchCatalog {
  target_classes: string[]
  zones: string[]
}

export interface SearchRoute { drone_id: number; connection_epoch: number; frame: 'map_enu'; waypoints: Array<[number, number, number]> }

export interface SearchPreview {
  session: string
  intent_id: string
  t: number
  expiresAt: number
  routes: SearchRoute[]
  preview: {
    zone_id: string
    target_class: string | null
    mode: 'search' | 'survey'
    allocations: Array<{ drone_id: number; source_id: string; task_id: string; workload_cells: number; lane_count: number }>
  }
}

export interface SearchCell { cell_id: string; x_m: number; y_m: number; z_m: number; floor_id: string }
export interface SearchTaskStatus { drone_id: number; task_id: string; state: string; covered_cells: number; total_cells: number; covered_cell_ids: string[]; cells: SearchCell[] }
export interface SearchFinding { sighting_id: string; source_id: string; acknowledged: boolean; label: string; confidence: number; bbox_xyxy: number[]; observation_count: number; frame: { frame_id: string; source_id: string; mission_id: string; worker_run_id: string | null; frame_sequence: number; decoded_at_monotonic_s: number; evaluated_at_monotonic_s: number } | null; position: { x_m: number; y_m: number; z_m: number; zone_id: string; floor_id: string } | null }
export interface SearchStatus { session: string; intent_id: string; state: string; mode: 'search' | 'survey'; tasks: SearchTaskStatus[]; candidates: SearchFinding[]; detection_workers?: Array<{ drone_id: number; state: string; failure_reason?: string | null }> }
export interface SearchResolution { session: string; correlation_id: string; source: 'template'; status: 'resolved' | 'clarify'; detail: string; zone_id: string | null; target_class: string | null; mode: 'search' | 'survey' }
export interface SearchClient { resolve?(sessionId: string, query: string): Promise<SearchResolution>; catalog(sessionId: string): Promise<SearchCatalog>; preview(intent: IntentV1): Promise<SearchPreview>; status(sessionId: string, intentId: string): Promise<SearchStatus>; acknowledge(sessionId: string, intentId: string, sightingId: string): Promise<SearchStatus> }

export class HttpSearchClient implements SearchClient {
  private readonly config: { baseUrl: string; token: string }
  private readonly fetcher: typeof fetch

  constructor(config: { baseUrl: string; token: string }, fetcher: typeof fetch = fetch) {
    this.config = config
    this.fetcher = fetcher
  }
  async resolve(sessionId: string, query: string): Promise<SearchResolution> {
    const value = await this.request(sessionId, 'resolve', { query })
    if (!record(value) || value.session !== sessionId || !text(value.correlation_id) || value.source !== 'template' ||
        !text(value.detail) || !['resolved', 'clarify'].includes(String(value.status)) ||
        (value.status === 'resolved' ? !text(value.zone_id) || !['search', 'survey'].includes(String(value.mode ?? 'search')) || (value.mode !== 'survey' && !text(value.target_class)) || (value.mode === 'survey' && value.target_class !== null) : value.zone_id !== null || value.target_class !== null)) {
      throw new Error('The relay returned an invalid search interpretation.')
    }
    return { ...value, mode: (value.mode ?? 'search') as 'search' | 'survey' } as unknown as SearchResolution
  }
  async catalog(sessionId: string): Promise<SearchCatalog> {
    const value = await this.request(sessionId, 'catalog')
    if (!record(value) || value.session !== sessionId || !strings(value.target_classes) || !strings(value.zones)) throw new Error('The relay returned an invalid search catalog.')
    return { target_classes: value.target_classes, zones: value.zones }
  }
  async preview(intent: IntentV1): Promise<SearchPreview> {
    if (intent.name !== 'search') throw new Error('Only search intents can be previewed.')
    const args = intent.args as SearchArgs
    const value = await this.request(intent.session, 'preview', { intent: { ...intent, confirm: true } })
    if (!record(value) || value.session !== intent.session || value.intent_id !== intent.intent_id || !integer(value.t) || !integer(value.expires_at_ms) || !record(value.preview) || value.preview.zone_id !== args.zone_id || value.preview.target_class !== ('target_class' in args ? args.target_class : null) || (value.preview.mode ?? 'search') !== ('mode' in args ? args.mode : 'search') || !Array.isArray(value.preview.allocations) || !value.preview.allocations.every(allocation) || !routes(value.routes, intent.selection)) throw new Error('The relay returned an invalid search preview.')
    return { session: value.session as string, intent_id: value.intent_id as string, t: value.t, expiresAt: value.expires_at_ms, routes: value.routes as SearchRoute[], preview: { zone_id: value.preview.zone_id as string, target_class: value.preview.target_class as string | null, mode: (value.preview.mode ?? 'search') as 'search' | 'survey', allocations: value.preview.allocations as SearchPreview['preview']['allocations'] } }
  }
  async status(sessionId: string, intentId: string): Promise<SearchStatus> { return this.parseStatus(await this.request(sessionId, encodeURIComponent(intentId)), sessionId, intentId) }
  async acknowledge(sessionId: string, intentId: string, sightingId: string): Promise<SearchStatus> { return this.parseStatus(await this.request(sessionId, `${encodeURIComponent(intentId)}/findings/${encodeURIComponent(sightingId)}/ack`, {}), sessionId, intentId) }
  private parseStatus(value: unknown, sessionId: string, intentId: string): SearchStatus {
    if (!record(value) || value.session !== sessionId || value.intent_id !== intentId || !text(value.state) || !['search', 'survey'].includes(String(value.mode ?? 'search')) || !Array.isArray(value.tasks) || !value.tasks.every(task) || !Array.isArray(value.candidates) || !value.candidates.every(finding) || (value.detection_workers !== undefined && (!Array.isArray(value.detection_workers) || !value.detection_workers.every(worker)))) throw new Error('The relay returned an invalid search status.')
    return { ...value, mode: (value.mode ?? 'search') as 'search' | 'survey' } as unknown as SearchStatus
  }
  private async request(session: string, action: string, body?: object): Promise<unknown> {
    const url = new URL(this.config.baseUrl)
    url.protocol = url.protocol === 'wss:' || url.protocol === 'https:' ? 'https:' : 'http:'
    url.pathname = `/session/${encodeURIComponent(session)}/search/${action}`; url.search = ''; url.hash = ''
    const response = await this.fetcher(url.toString(), { method: body ? 'POST' : 'GET', headers: { Authorization: `Bearer ${this.config.token}`, 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) })
    const value: unknown = await response.json()
    if (!response.ok) throw new Error(record(value) && text(value.detail) ? value.detail : 'Search is unavailable.')
    return value
  }
}
function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) }
function text(value: unknown): value is string { return typeof value === 'string' && value.length > 0 }
function number(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) }
function integer(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0 }
function strings(value: unknown): value is string[] { return Array.isArray(value) && value.every(text) }
function cell(value: unknown): boolean { return record(value) && text(value.cell_id) && text(value.floor_id) && number(value.x_m) && number(value.y_m) && number(value.z_m) }
function task(value: unknown): boolean { return record(value) && Number.isInteger(value.drone_id) && text(value.task_id) && text(value.state) && Number.isInteger(value.covered_cells) && Number.isInteger(value.total_cells) && strings(value.covered_cell_ids) && Array.isArray(value.cells) && value.cells.every(cell) }
function allocation(value: unknown): boolean { return record(value) && Number.isInteger(value.drone_id) && text(value.source_id) && text(value.task_id) && Number.isInteger(value.workload_cells) && Number.isInteger(value.lane_count) }
function finding(value: unknown): boolean { if (!record(value) || !text(value.sighting_id) || !text(value.source_id) || typeof value.acknowledged !== 'boolean' || !text(value.label) || !number(value.confidence) || !Array.isArray(value.bbox_xyxy) || !value.bbox_xyxy.every(number) || !Number.isInteger(value.observation_count)) return false; if (value.position !== null && (!record(value.position) || !number(value.position.x_m) || !number(value.position.y_m) || !number(value.position.z_m) || !text(value.position.zone_id) || !text(value.position.floor_id))) return false; return value.frame === null || (record(value.frame) && text(value.frame.frame_id) && text(value.frame.source_id) && text(value.frame.mission_id) && (typeof value.frame.worker_run_id === 'string' || value.frame.worker_run_id === null) && Number.isInteger(value.frame.frame_sequence) && number(value.frame.decoded_at_monotonic_s) && number(value.frame.evaluated_at_monotonic_s)) }
function worker(value: unknown): boolean { return record(value) && Number.isInteger(value.drone_id) && text(value.state) && (value.failure_reason === undefined || value.failure_reason === null || text(value.failure_reason)) }

function routes(value: unknown, selected: readonly number[]): boolean {
  if (!Array.isArray(value) || value.length !== selected.length || value.length > 4) return false
  const ids = new Set<number>()
  let points = 0
  for (const route of value) {
    if (!record(route) || !integer(route.drone_id) || !selected.includes(route.drone_id) || ids.has(route.drone_id) ||
        !integer(route.connection_epoch) || route.connection_epoch < 1 || route.frame !== 'map_enu' ||
        !Array.isArray(route.waypoints) || route.waypoints.length < 1) return false
    ids.add(route.drone_id)
    points += route.waypoints.length
    if (points > 4096 || !route.waypoints.every((point) => Array.isArray(point) && point.length === 3 && point.every(number))) return false
  }
  return true
}
