import { isRecord, PlatformHttp } from '../platform/http'

export interface DetectionBox { label: string; confidence: number; bbox_xyxy: [number, number, number, number] }
export interface DetectionFrame { sequence: number; width: number; height: number; jpeg_base64: string; age_ms: number; detections: DetectionBox[] }
export interface DetectionSnapshot { state: string; frame: DetectionFrame | null }
export interface DetectionSource { drone_id: number; connection_epoch: number; camera_id: string; stream: string }
export interface LiveDetectionClient { snapshot(source: DetectionSource, signal?: AbortSignal): Promise<DetectionSnapshot> }

export class HttpLiveDetectionClient implements LiveDetectionClient {
  private readonly http: PlatformHttp
  constructor(http: PlatformHttp) { this.http = http }
  async snapshot(source: DetectionSource, signal?: AbortSignal): Promise<DetectionSnapshot> {
    const value = await this.http.request(`/live-detections/${source.drone_id}/${encodeURIComponent(source.camera_id)}/${source.connection_epoch}`, undefined, signal)
    if (!isRecord(value) || value.session !== this.http.connection.sessionId || value.device_id !== source.drone_id ||
      value.connection_epoch !== source.connection_epoch || value.camera_id !== source.camera_id || value.stream !== source.stream ||
      typeof value.state !== 'string' || !['unconfigured', 'stopped', 'stopping', 'starting', 'waiting_for_frame', 'live', 'stale', 'failed'].includes(value.state) ||
      (value.state === 'live' ? !isFrame(value.frame) : value.frame !== null)) {
      throw new Error('The relay returned a mismatched camera detection frame.')
    }
    return { state: value.state, frame: value.frame as DetectionFrame | null }
  }
}

function isFrame(value: unknown): value is DetectionFrame {
  if (!isRecord(value) || !positiveInteger(value.sequence) || !positiveInteger(value.width) || !positiveInteger(value.height) ||
      value.width > 1920 || value.height > 1920 || value.width * value.height > 1920 * 1080 ||
      !finite(value.age_ms) || value.age_ms < 0 || value.age_ms > 1500 ||
      typeof value.jpeg_base64 !== 'string' || value.jpeg_base64.length > 1398104 ||
      !/^\/9j\/[A-Za-z0-9+/]*={0,2}$/.test(value.jpeg_base64) ||
      !Array.isArray(value.detections) || value.detections.length > 256) return false
  const width = value.width, height = value.height
  return value.detections.every((box) => isRecord(box) && typeof box.label === 'string' && box.label.length > 0 && box.label.length <= 100 &&
    finite(box.confidence) && box.confidence >= 0 && box.confidence <= 1 && Array.isArray(box.bbox_xyxy) && box.bbox_xyxy.length === 4 &&
    box.bbox_xyxy.every(finite) && box.bbox_xyxy[0] >= 0 && box.bbox_xyxy[1] >= 0 &&
    box.bbox_xyxy[2] > box.bbox_xyxy[0] && box.bbox_xyxy[2] <= width && box.bbox_xyxy[3] > box.bbox_xyxy[1] && box.bbox_xyxy[3] <= height)
}
function finite(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) }
function positiveInteger(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) > 0 }
