import type { MapDraft } from '../modules/map/authoring/types'
import { MAX_IMAGE_BYTES } from '../modules/map/authoring/files'
import { isSurveyAreaId, isSurveyCandidateId } from '../relay/survey'
import { isRecord, PlatformHttp } from './http'

const MAX_PREVIEW = 24 * 1024 * 1024
const MAX_CANDIDATE_IMAGE = 16 * 1024 * 1024
const FILES = ['recording/recording.json', 'recording/observations.jsonl', 'occupancy/manifest.json', 'occupancy/occupancy.png', 'pose_path.json', 'tag_candidates.json']
export interface SurveyCandidateReference {
  candidateId: string; session: string; intentId: string; runId: string
  deviceId: number; connectionEpoch: number; areaId: string
}
export interface SurveyCandidatePreview {
  reference: SurveyCandidateReference
  image: { dataUrl: string; width: number; height: number; sha256: string; bytes: number }
  frame: string; resolutionM: number; originXM: number; originYM: number; createdAt: number
  source: { source_id: string; odom_frame: string; lidar_frame: string; mount_id: string }
  /** Exact verified response preserved for evidence download; it grants no navigation authority. */
  evidence: Record<string, unknown>
}
export interface SurveyCandidateClient {
  load(reference: SurveyCandidateReference, signal?: AbortSignal): Promise<SurveyCandidatePreview>
}
const invalid = (): never => { throw new Error('The relay returned incompatible survey candidate evidence.') }
const record = (value: unknown) => isRecord(value) ? value : invalid()
const text = (value: unknown): string => isSurveyAreaId(value) ? value : invalid()
const positive = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : invalid()
const finite = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) ? value : invalid()
const integer = (value: unknown): number => Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : invalid()
const digest = (value: unknown): string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : invalid()
function inventory(value: unknown): { bytes: number; sha256: string } {
  const entry = record(value)
  if (Object.keys(entry).length !== 2) invalid()
  const bytes = integer(entry.bytes)
  if (bytes > MAX_CANDIDATE_IMAGE) invalid()
  return { bytes, sha256: digest(entry.sha256) }
}

export class HttpSurveyCandidateClient implements SurveyCandidateClient {
  private readonly http: PlatformHttp
  constructor(http: PlatformHttp) { this.http = http }
  async load(reference: SurveyCandidateReference, signal?: AbortSignal): Promise<SurveyCandidatePreview> {
    // Snapshot caller-owned reference before the first await.
    const expected = { ...reference }
    if (!isSurveyCandidateId(expected.candidateId) || expected.session !== this.http.connection.sessionId ||
      !isSurveyAreaId(expected.areaId) || !isSurveyAreaId(expected.intentId) || !isSurveyAreaId(expected.runId) ||
      !Number.isSafeInteger(expected.deviceId) || expected.deviceId < 1 ||
      !Number.isSafeInteger(expected.connectionEpoch) || expected.connectionEpoch < 1) invalid()
    const raw = record(await this.http.request(`/survey-candidates/${encodeURIComponent(expected.candidateId)}`, undefined, signal, MAX_PREVIEW))
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    if (raw.v !== 1 || raw.type !== 'survey_candidate_preview' || raw.navigation_authority !== false ||
      raw.candidate_id !== expected.candidateId || raw.session !== expected.session || raw.intent_id !== expected.intentId ||
      raw.run_id !== expected.runId || raw.device_id !== expected.deviceId || raw.connection_epoch !== expected.connectionEpoch || raw.area_id !== expected.areaId) invalid()
    const { image: rawImage, ...metadata } = raw
    if (new TextEncoder().encode(JSON.stringify(metadata)).length > 64 * 1024) invalid()
    const sourceRaw = record(raw.source)
    const source = { source_id: text(sourceRaw.source_id), odom_frame: text(sourceRaw.odom_frame), lidar_frame: text(sourceRaw.lidar_frame), mount_id: text(sourceRaw.mount_id) }
    if (source.odom_frame === source.lidar_frame || source.odom_frame === 'world' || source.lidar_frame === 'world') invalid()
    const pose = record(raw.pose_identity)
    if (pose.session !== expected.session || pose.connection_epoch !== expected.connectionEpoch || pose.frame !== source.odom_frame) invalid()
    text(pose.event_id); text(pose.source_id)
    const occupancy = record(raw.occupancy), grid = record(occupancy.grid), origin = record(grid.origin)
    const input = record(occupancy.input), inputSource = record(input.source)
    if (occupancy.format !== 'ohmni.canonical-occupancy-grid.v1' || input.run_id !== expected.runId ||
      Object.keys(inputSource).length !== 6 || inputSource.transport !== 'file_jsonl' ||
      inputSource.session !== expected.session || inputSource.device_id !== expected.deviceId || inputSource.connection_epoch !== expected.connectionEpoch ||
      inputSource.source_id !== source.source_id || inputSource.node_type !== 'ground' || occupancy.mount_id !== source.mount_id ||
      grid.frame !== source.odom_frame || grid.frame_kind !== 'source_scoped_local_odometry' || grid.registered_to_world !== false || grid.png_row_0 !== 'maximum_y') invalid()
    const files = record(raw.files)
    if (Object.keys(files).length !== FILES.length || FILES.some((name) => !(name in files))) invalid()
    for (const name of FILES) inventory(files[name])
    if (digest(input.observations_sha256) !== inventory(files['recording/observations.jsonl']).sha256) invalid()
    const image = record(rawImage), imageFile = inventory(files['occupancy/occupancy.png'])
    const occupancyImage = inventory(record(occupancy.files)['occupancy.png'])
    const bytes = integer(image.bytes), sha256 = digest(image.sha256)
    if (Object.keys(image).length !== 4 || image.mime_type !== 'image/png' || bytes < 33 || bytes > MAX_CANDIDATE_IMAGE ||
      bytes !== imageFile.bytes || sha256 !== imageFile.sha256 || bytes !== occupancyImage.bytes || sha256 !== occupancyImage.sha256 ||
      typeof image.data_base64 !== 'string' || image.data_base64.length !== 4 * Math.ceil(bytes / 3) ||
      !/^[A-Za-z0-9+/]*={0,2}$/.test(image.data_base64)) invalid()
    let binary: string
    try { binary = atob(image.data_base64 as string) } catch { return invalid() }
    if (binary.length !== bytes || btoa(binary) !== image.data_base64) invalid()
    const data = Uint8Array.from(binary, (c) => c.charCodeAt(0))
    const actualHash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)), (b) => b.toString(16).padStart(2, '0')).join('')
    if (actualHash !== sha256 || [137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82].some((v, i) => data[i] !== v)) invalid()
    const view = new DataView(data.buffer), width = integer(grid.width), height = integer(grid.height)
    if (width < 1 || height < 1 || width * height > 16_777_216 || view.getUint32(16) !== width || view.getUint32(20) !== height) invalid()
    const dataUrl = `data:image/png;base64,${image.data_base64}`
    await verifyDecodedImage(dataUrl, width, height, signal)
    return { reference: expected, image: { dataUrl, width, height, sha256, bytes }, frame: source.odom_frame,
      resolutionM: positive(grid.resolution_m), originXM: finite(origin.x_m), originYM: finite(origin.y_m),
      createdAt: integer(occupancy.created_at_ms), source, evidence: raw }
  }
}

function verifyDecodedImage(dataUrl: string, width: number, height: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    const finish = (error?: Error) => {
      clearTimeout(timeout); signal?.removeEventListener('abort', abort)
      image.onload = null; image.onerror = null
      if (error) { image.src = ''; reject(error) } else resolve()
    }
    const abort = () => finish(new DOMException('Aborted', 'AbortError'))
    const timeout = setTimeout(() => finish(new Error('The candidate image could not be decoded in time.')), 10_000)
    signal?.addEventListener('abort', abort, { once: true })
    image.onload = () => image.naturalWidth === width && image.naturalHeight === height ? finish() : finish(new Error('The candidate image dimensions do not match its bytes.'))
    image.onerror = () => finish(new Error('The candidate occupancy PNG could not be decoded.'))
    if (signal?.aborted) abort(); else image.src = dataUrl
  })
}

/** Importable draft preserves the source frame and intentionally lacks world registration. */
export function surveyCandidateDraft(candidate: SurveyCandidatePreview): MapDraft {
  if (candidate.image.bytes > MAX_IMAGE_BYTES) throw new Error('Editable Map drafts accept occupancy images up to 8 MiB. Download this candidate evidence separately.')
  return { format: 'sweep-map-draft-v1', metadata: {
    mapVersion: candidate.reference.candidateId, floorId: '', frame: candidate.frame, units: 'm',
    resolutionM: candidate.resolutionM, originXM: candidate.originXM, originYM: candidate.originYM,
    createdAt: candidate.createdAt, creationEvidence: JSON.stringify({ ...candidate.reference, source: candidate.source, imageSha256: candidate.image.sha256, registeredToWorld: false }),
  }, image: { name: `${candidate.reference.candidateId}.png`, dataUrl: candidate.image.dataUrl,
    width: candidate.image.width, height: candidate.image.height, sha256: candidate.image.sha256 }, features: [], tags: [] }
}

export function downloadSurveyEvidence(candidate: SurveyCandidatePreview): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(candidate.evidence, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = `${candidate.reference.candidateId}.json`; link.click()
  URL.revokeObjectURL(url)
}
