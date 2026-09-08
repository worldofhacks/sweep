import type { NavigationExecutionEvidence, NavigationRoute } from '../navigation/types'
import { isNavigationExecution, isNavigationRoute } from '../navigation/validation'
import { isRecord, PlatformHttp } from '../platform/http'

export interface MultiviewTarget { id: number; deviceClass: 'aircraft'; epoch: number }
export interface MultiviewViewpoint { viewpointId: string; zoneId: string; captureId: string }
export interface MultiviewPreviewRequest {
  intentId: string
  selected: [MultiviewTarget]
  viewpoints: MultiviewViewpoint[]
}
export interface MultiviewPreview {
  previewId: string
  intentId: string
  expiresAt: number
  previewHash: string
  execution: NavigationExecutionEvidence
  views: Array<MultiviewViewpoint & { route: NavigationRoute; capture: { roomId: string; pattern: 'single_still' } }>
}
export interface MultiviewStatus {
  workflowId: string
  intentId: string
  status: 'pending_confirmation' | 'navigating' | 'failed' | 'completed'
  views: Array<MultiviewViewpoint & {
    state: 'planned' | 'navigating' | 'arrival_verified' | 'capturing' | 'completed' | 'failed'
    detail: string
  }>
}
export interface MultiviewClient {
  preview(request: MultiviewPreviewRequest): Promise<MultiviewPreview>
  confirm(preview: MultiviewPreview): Promise<string>
  status(preview: MultiviewPreview): Promise<MultiviewStatus>
}

const id = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 128

export class HttpMultiviewClient implements MultiviewClient {
  private readonly http: PlatformHttp
  private readonly now: () => number
  private retained: string | null = null
  private sequence = 0
  constructor(http: PlatformHttp, now = Date.now) { this.http = http; this.now = now }

  async preview(request: MultiviewPreviewRequest): Promise<MultiviewPreview> {
    const sequence = ++this.sequence
    this.retained = null
    const expected: MultiviewPreviewRequest = structuredClone(request)
    const started = this.now()
    const raw = await this.http.request('/multiview/preview', expected)
    const ended = this.now()
    if (sequence !== this.sequence) throw new Error('A newer photo-route review replaced this request.')
    if (!isRecord(raw) || !id(raw.previewId) || raw.intentId !== expected.intentId ||
      typeof raw.previewHash !== 'string' || !/^[a-f0-9]{64}$/.test(raw.previewHash) ||
      !Number.isSafeInteger(raw.expiresAt) || !Number.isSafeInteger(raw.serverNowMs) || ended < started ||
      !isNavigationExecution(raw.execution) || !Array.isArray(raw.views) || raw.views.length !== expected.viewpoints.length ||
      raw.views.length < 1 || raw.views.length > 8) throw new Error('The relay returned an invalid photo-route review.')
    const remaining = Number(raw.expiresAt) - Number(raw.serverNowMs) - (ended - started)
    if (remaining <= 0 || remaining > 60_000) throw new Error('The photo-route review expired before it arrived.')
    for (const [index, view] of raw.views.entries()) {
      const wanted = expected.viewpoints[index]
      if (!isRecord(view) || view.viewpointId !== wanted.viewpointId || view.zoneId !== wanted.zoneId || view.captureId !== wanted.captureId ||
        !isNavigationRoute(view.route) || !isRecord(view.route.target) || view.route.target.id !== expected.selected[0].id || view.route.target.epoch !== expected.selected[0].epoch || view.route.target.deviceClass !== 'aircraft' ||
        !isRecord(view.route.arrivalSlot) || view.route.arrivalSlot.zoneId !== wanted.zoneId ||
        !isRecord(view.capture) || view.capture.roomId !== wanted.zoneId || view.capture.pattern !== 'single_still') {
        throw new Error('The photo-route review does not match the requested aircraft and stops.')
      }
    }
    const preview = { ...raw, expiresAt: ended + remaining } as unknown as MultiviewPreview
    this.retained = JSON.stringify(preview)
    return preview
  }

  async confirm(preview: MultiviewPreview): Promise<string> {
    if (this.retained !== JSON.stringify(preview) || preview.expiresAt <= this.now()) throw new Error('Preview the photo route again before confirming.')
    this.retained = null
    const result = await this.http.request('/multiview/confirm', { previewId: preview.previewId, intentId: preview.intentId, previewHash: preview.previewHash })
    if (!isRecord(result) || result.status !== 'accepted' || result.code !== 'multiview_accepted' || result.workflowId !== preview.previewId) {
      throw new Error('The relay did not accept the reviewed photo route.')
    }
    return result.workflowId
  }

  async status(preview: MultiviewPreview): Promise<MultiviewStatus> {
    const raw = await this.http.request(`/multiview/${encodeURIComponent(preview.previewId)}`)
    if (!isRecord(raw) || raw.workflowId !== preview.previewId || raw.intentId !== preview.intentId ||
      !['pending_confirmation', 'navigating', 'failed', 'completed'].includes(String(raw.status)) ||
      !Array.isArray(raw.views) || raw.views.length !== preview.views.length || !raw.views.every((view, index) => {
        const wanted = preview.views[index]
        return isRecord(view) && view.viewpointId === wanted.viewpointId && view.captureId === wanted.captureId && view.zoneId === wanted.zoneId &&
          ['planned', 'navigating', 'arrival_verified', 'capturing', 'completed', 'failed'].includes(String(view.state)) && typeof view.detail === 'string' && view.detail.length <= 2048
      })) throw new Error('The relay returned status for a different photo route.')
    return raw as unknown as MultiviewStatus
  }
}
