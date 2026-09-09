import type { CaptureRequestContext, CaptureResponseTarget, Reconstruction, SpaceDetail, SpaceRequest, SurfaceRequest } from './types'

export function isCurrentSurfaceRequest(item: SurfaceRequest, job: Reconstruction): boolean {
  return job.status === 'ready' && item.job_id === job.id && item.artifact_sha256 === job.artifact_sha256
    && Boolean(job.surface_review?.regions.some(region => region.id === item.region_id))
}

export function isActionableRequest(item: SpaceRequest | SurfaceRequest, detail: SpaceDetail): boolean {
  return detail.space.status === 'active' && item.status === 'open'
    && ('cell_id' in item || isCurrentSurfaceRequest(item, detail.reconstruction))
}

export function spaceCaptureRequests(detail: SpaceDetail): (SpaceRequest | SurfaceRequest)[] {
  return [...detail.requests, ...(detail.surface_requests ?? [])].sort((a, b) => b.created_at - a.created_at
    || JSON.stringify(captureRequestContext(a).target).localeCompare(JSON.stringify(captureRequestContext(b).target)))
}

export function responseMatches(expected: CaptureResponseTarget, actual: unknown): boolean {
  if (!actual || typeof actual !== 'object') return false
  const value = actual as Record<string, unknown>
  return expected.kind === value.kind && (expected.kind === 'location'
    ? expected.cell_id === value.cell_id
    : expected.job_id === value.job_id && expected.artifact_sha256 === value.artifact_sha256 && expected.region_id === value.region_id)
}

export function captureRequestContext(item: SpaceRequest | SurfaceRequest): CaptureRequestContext {
  return 'cell_id' in item
    ? { target: { kind: 'location', cell_id: item.cell_id }, label: 'Requested viewpoint', note: item.note }
    : { target: { kind: 'surface', job_id: item.job_id, artifact_sha256: item.artifact_sha256, region_id: item.region_id }, label: item.label, note: item.note }
}

export function linkedCaptureIds(detail: SpaceDetail, target: CaptureResponseTarget): string[] {
  const requests = target.kind === 'location' ? detail.requests : detail.surface_requests ?? []
  return requests.find(item => responseMatches(target, captureRequestContext(item).target))?.capture_ids ?? []
}
