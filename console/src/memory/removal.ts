export interface RemovalReceipt {
  capture_id: string
  state: 'cleanup_pending' | 'local_removed'
  requested_at: number
  requested_by: string
  completed_at: number | null
  recordings: number
  builds: number
  analysis_pending: boolean
}
export interface RemovalPreview {
  capture_id: string
  state: 'preview'
  confirmation: string
  recordings: number
  builds: number
  analysis_pending: boolean
}
export interface RemovalDirectory {
  receipts: RemovalReceipt[]
  next_before: number | null
  pending: number
  completed: number
  scope: 'space' | 'own'
}
export function readRemoval(value: unknown, capture?: string): RemovalReceipt | RemovalPreview {
  if (!value || typeof value !== 'object') throw new Error('Removal status could not be verified.')
  const item = value as Record<string, unknown>
  const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
  const timestamp = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 8.64e15
  if (typeof item.capture_id !== 'string' || (capture && item.capture_id !== capture) || !count(item.recordings) || !count(item.builds) || typeof item.analysis_pending !== 'boolean')
    throw new Error('Removal status could not be verified.')
  if (item.state === 'preview' && typeof item.confirmation === 'string' && /^[a-f0-9]{64}$/.test(item.confirmation)) return item as unknown as RemovalPreview
  if (typeof item.requested_by === 'string' && timestamp(item.requested_at) && ((item.state === 'cleanup_pending' && item.completed_at === null) || (item.state === 'local_removed' && timestamp(item.completed_at) && item.completed_at >= item.requested_at)))
    return item as unknown as RemovalReceipt
  throw new Error('Removal status could not be verified.')
}
