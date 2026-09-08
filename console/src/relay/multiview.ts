export interface MultiviewTarget {
  id: number
  deviceClass: 'aircraft'
  epoch: number
}

export interface MultiviewViewpoint {
  viewpointId: string
  zoneId: string
  captureId: string
}

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
  execution: Record<string, unknown>
  views: Array<MultiviewViewpoint & {
    route: Record<string, unknown>
    capture: { roomId: string; pattern: 'single_still' }
  }>
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
