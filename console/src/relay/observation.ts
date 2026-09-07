export const MAX_OBSERVATION_BYTES = 64 * 1024
export const MAX_OBSERVATION_COORDINATE_M = 1_000_000

export type ObservationNodeType = 'aircraft' | 'ground'
export type ObservationPayloadKind =
  | 'telemetry'
  | 'pose'
  | 'range_scan'
  | 'camera_frame'
  | 'tag_observation'
  | 'status'

export interface SourceTime {
  readonly clock_id: string
  readonly unit: 'ms' | 'ns'
  readonly value: number
}

export interface FramedVector {
  readonly frame: string
  readonly x_m: number
  readonly y_m: number
  readonly z_m: number
}

export interface FramedPose {
  readonly parent_frame: string
  readonly child_frame: string
  readonly x_m: number
  readonly y_m: number
  readonly z_m: number
  readonly qx: number
  readonly qy: number
  readonly qz: number
  readonly qw: number
}

export interface TelemetryPayload {
  readonly kind: 'telemetry'
  readonly position: FramedVector
  readonly velocity: Readonly<{
    frame: string
    x_m_s: number
    y_m_s: number
    z_m_s: number
  }>
  readonly battery: number
  readonly link: number
  readonly pos_quality: number
  readonly state: string
}

export interface PoseCaptureAlignment {
  readonly v: 1
  readonly alignment_config_id: string
  readonly alignment_config_sha256: string
  readonly kinematic_calibration_id: string
  readonly kinematic_calibration_sha256: string
  readonly frame_pts: SourceTime
  readonly gimbal_receipt: SourceTime
  readonly body_attitude_receipt: SourceTime
  readonly gimbal_attitude: Readonly<{ yaw_deg: number; pitch_deg: number; roll_deg: number }>
  readonly body_attitude: Readonly<{ yaw_deg: number; pitch_deg: number; roll_deg: number }>
  readonly frame_capture_error_ms: number
  readonly gimbal_callback_latency_ms: number
  readonly body_attitude_callback_latency_ms: number
  readonly gimbal_callback_orientation_error_deg: number
  readonly body_attitude_callback_orientation_error_deg: number
  readonly gimbal_angular_rate_bound_deg_s: number
  readonly body_angular_rate_bound_deg_s: number
  readonly max_extrinsics_angle_error_deg: number
}

export interface PosePayload {
  readonly kind: 'pose'
  readonly pose: FramedPose
  readonly capture_alignment?: PoseCaptureAlignment
}

export interface RangeScanPayload {
  readonly kind: 'range_scan'
  readonly sensor_pose: FramedPose
  readonly angle_min_rad: number
  readonly angle_increment_rad: number
  readonly range_min_m: number
  readonly range_max_m: number
  readonly ranges_m: readonly (number | null)[]
  readonly mount_id: string
}

export interface CameraFramePayload {
  readonly kind: 'camera_frame'
  readonly image_id: string
  readonly sha256: string
  readonly width_px: number
  readonly height_px: number
  readonly calibration_id: string
}

export type TagObservationReason =
  | 'pose'
  | 'ambiguous'
  | 'unconfigured_tag'
  | 'duplicate_tag'
  | 'tag_too_small'
  | 'reprojection_or_cheirality'

export interface TagObservationPayload {
  readonly kind: 'tag_observation'
  readonly family: 'tag36h11'
  readonly tag_id: number
  readonly image_id: string
  readonly pose_accepted: boolean
  readonly tag_pose: FramedPose | null
  readonly covariance_m2: readonly number[] | null
  readonly reason: TagObservationReason
  readonly size_m: number | null
  readonly corners_px: readonly (readonly [number, number])[]
  readonly pixel_frame: 'camera' | 'rectified_camera'
  readonly reprojection_rms_px: number | null
}

export interface StatusPayload {
  readonly kind: 'status'
  readonly code: string
  readonly detail: string
  readonly capabilities: readonly string[]
}

export type ObservationPayload =
  | TelemetryPayload
  | PosePayload
  | RangeScanPayload
  | CameraFramePayload
  | TagObservationPayload
  | StatusPayload

export interface Observation {
  readonly v: 1
  readonly type: 'observation'
  readonly event_id: string
  readonly session: string
  readonly device_id: number
  readonly connection_epoch: number
  readonly source_id: string
  readonly node_type: ObservationNodeType
  readonly frame: string
  readonly confidence: number
  readonly t_capture: SourceTime | null
  readonly t_source_receipt: SourceTime
  readonly clock_mapping_id: string | null
  readonly payload: ObservationPayload
  readonly t_ingest: number
}

type RecordValue = Record<string, unknown>

const eventFields = new Set([
  'v',
  'type',
  'event_id',
  'session',
  'device_id',
  'connection_epoch',
  'source_id',
  'node_type',
  'frame',
  'confidence',
  't_capture',
  't_source_receipt',
  'clock_mapping_id',
  'payload',
  't_ingest',
])

function record(value: unknown): RecordValue | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null ? (value as RecordValue) : null
}

function exact(value: unknown, fields: ReadonlySet<string>): RecordValue | null {
  const result = record(value)
  if (!result) return null
  const keys = Object.keys(result)
  return keys.length === fields.size && keys.every((key) => fields.has(key)) ? result : null
}

function text(value: unknown, maximum = 128): string | null {
  if (typeof value !== 'string' || value.length === 0 || Array.from(value).length > maximum) return null
  if (value.trim() !== value || /[\p{C}\p{Zl}\p{Zp}]/u.test(value)) return null
  return value
}

function integer(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : null
}

function number(value: unknown, maximum = MAX_OBSERVATION_COORDINATE_M): number | null {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= maximum ? value : null
}

function unitInterval(value: unknown): number | null {
  const result = number(value, 1)
  return result !== null && result >= 0 ? result : null
}

function sourceTime(value: unknown): SourceTime | null {
  const result = exact(value, new Set(['clock_id', 'unit', 'value']))
  if (!result) return null
  const clockId = text(result.clock_id, 128)
  const timestamp = integer(result.value)
  if (!clockId || timestamp === null || (result.unit !== 'ms' && result.unit !== 'ns')) return null
  return freeze({ clock_id: clockId, unit: result.unit, value: timestamp })
}

function framedVector(value: unknown): FramedVector | null {
  const result = exact(value, new Set(['frame', 'x_m', 'y_m', 'z_m']))
  if (!result) return null
  const frame = text(result.frame)
  const x = number(result.x_m)
  const y = number(result.y_m)
  const z = number(result.z_m)
  return frame && x !== null && y !== null && z !== null ? freeze({ frame, x_m: x, y_m: y, z_m: z }) : null
}

function framedPose(value: unknown): FramedPose | null {
  const result = exact(value, new Set(['parent_frame', 'child_frame', 'x_m', 'y_m', 'z_m', 'qx', 'qy', 'qz', 'qw']))
  if (!result) return null
  const parentFrame = text(result.parent_frame)
  const childFrame = text(result.child_frame)
  const x = number(result.x_m)
  const y = number(result.y_m)
  const z = number(result.z_m)
  const qx = number(result.qx)
  const qy = number(result.qy)
  const qz = number(result.qz)
  const qw = number(result.qw)
  if (!parentFrame || !childFrame || parentFrame === childFrame || x === null || y === null || z === null || qx === null || qy === null || qz === null || qw === null) return null
  const norm = Math.hypot(qx, qy, qz, qw)
  return Math.abs(norm - 1) <= 1e-6
    ? freeze({ parent_frame: parentFrame, child_frame: childFrame, x_m: x, y_m: y, z_m: z, qx, qy, qz, qw })
    : null
}

function payload(value: unknown, frame: string): ObservationPayload | null {
  const raw = record(value)
  if (!raw || typeof raw.kind !== 'string') return null
  switch (raw.kind) {
    case 'telemetry':
      return telemetryPayload(raw, frame)
    case 'pose':
      return posePayload(raw, frame)
    case 'range_scan':
      return rangeScanPayload(raw, frame)
    case 'camera_frame':
      return cameraFramePayload(raw)
    case 'tag_observation':
      return tagObservationPayload(raw, frame)
    case 'status':
      return statusPayload(raw)
    default:
      return null
  }
}

function telemetryPayload(value: unknown, frame: string): TelemetryPayload | null {
  const result = exact(value, new Set(['kind', 'position', 'velocity', 'battery', 'link', 'pos_quality', 'state']))
  if (!result || result.kind !== 'telemetry') return null
  const position = framedVector(result.position)
  const velocity = exact(result.velocity, new Set(['frame', 'x_m_s', 'y_m_s', 'z_m_s']))
  const velocityFrame = velocity && text(velocity.frame)
  const vx = velocity && number(velocity.x_m_s)
  const vy = velocity && number(velocity.y_m_s)
  const vz = velocity && number(velocity.z_m_s)
  const battery = unitInterval(result.battery)
  const link = unitInterval(result.link)
  const posQuality = unitInterval(result.pos_quality)
  const state = text(result.state, 512)
  if (!position || position.frame !== frame || !velocityFrame || velocityFrame !== frame || vx === null || vy === null || vz === null || battery === null || link === null || posQuality === null || !state) return null
  return freeze({ kind: 'telemetry', position, velocity: freeze({ frame: velocityFrame, x_m_s: vx, y_m_s: vy, z_m_s: vz }), battery, link, pos_quality: posQuality, state })
}

function poseCaptureAlignment(value: unknown): PoseCaptureAlignment | null {
  const fields = new Set(['v', 'alignment_config_id', 'alignment_config_sha256', 'kinematic_calibration_id', 'kinematic_calibration_sha256', 'frame_pts', 'gimbal_receipt', 'body_attitude_receipt', 'gimbal_attitude', 'body_attitude', 'frame_capture_error_ms', 'gimbal_callback_latency_ms', 'body_attitude_callback_latency_ms', 'gimbal_callback_orientation_error_deg', 'body_attitude_callback_orientation_error_deg', 'gimbal_angular_rate_bound_deg_s', 'body_angular_rate_bound_deg_s', 'max_extrinsics_angle_error_deg'])
  const result = exact(value, fields)
  if (!result || result.v !== 1) return null
  const textFields = ['alignment_config_id', 'kinematic_calibration_id'] as const
  if (textFields.some(field => !text(result[field]))) return null
  const alignmentSha = typeof result.alignment_config_sha256 === 'string' && /^[0-9a-f]{64}$/.test(result.alignment_config_sha256) ? result.alignment_config_sha256 : null
  const calibrationSha = typeof result.kinematic_calibration_sha256 === 'string' && /^[0-9a-f]{64}$/.test(result.kinematic_calibration_sha256) ? result.kinematic_calibration_sha256 : null
  const times = [sourceTime(result.frame_pts), sourceTime(result.gimbal_receipt), sourceTime(result.body_attitude_receipt)]
  const attitude = (raw: unknown) => {
    const value = exact(raw, new Set(['yaw_deg', 'pitch_deg', 'roll_deg']))
    const yaw = value && number(value.yaw_deg)
    const pitch = value && number(value.pitch_deg)
    const roll = value && number(value.roll_deg)
    return yaw === null || pitch === null || roll === null ? null : freeze({ yaw_deg: yaw, pitch_deg: pitch, roll_deg: roll })
  }
  const gimbal = attitude(result.gimbal_attitude)
  const body = attitude(result.body_attitude)
  const numericFields = ['frame_capture_error_ms', 'gimbal_callback_latency_ms', 'body_attitude_callback_latency_ms', 'gimbal_callback_orientation_error_deg', 'body_attitude_callback_orientation_error_deg', 'gimbal_angular_rate_bound_deg_s', 'body_angular_rate_bound_deg_s', 'max_extrinsics_angle_error_deg'] as const
  const numbers = numericFields.map(field => number(result[field]))
  if (!alignmentSha || !calibrationSha || times.some(value => !value) || !gimbal || !body || numbers.some(value => value === null || value < 0)) return null
  if (times[0]!.clock_id !== 'dji_stream_presentation_ms' || times[0]!.unit !== 'ms' || [times[1]!, times[2]!].some(time => time.clock_id !== 'phone_elapsed_realtime_ms' || time.unit !== 'ms')) return null
  return freeze({
    v: 1,
    alignment_config_id: text(result.alignment_config_id)!,
    alignment_config_sha256: alignmentSha,
    kinematic_calibration_id: text(result.kinematic_calibration_id)!,
    kinematic_calibration_sha256: calibrationSha,
    frame_pts: times[0]!, gimbal_receipt: times[1]!, body_attitude_receipt: times[2]!,
    gimbal_attitude: gimbal, body_attitude: body,
    frame_capture_error_ms: numbers[0]!, gimbal_callback_latency_ms: numbers[1]!, body_attitude_callback_latency_ms: numbers[2]!,
    gimbal_callback_orientation_error_deg: numbers[3]!, body_attitude_callback_orientation_error_deg: numbers[4]!,
    gimbal_angular_rate_bound_deg_s: numbers[5]!, body_angular_rate_bound_deg_s: numbers[6]!, max_extrinsics_angle_error_deg: numbers[7]!,
  })
}

function posePayload(value: unknown, frame: string): PosePayload | null {
  const raw = record(value)
  if (!raw || (Object.keys(raw).length !== 2 && Object.keys(raw).length !== 3) || raw.kind !== 'pose') return null
  if (!('pose' in raw) || ('capture_alignment' in raw && Object.keys(raw).length !== 3)) return null
  const pose = framedPose(raw.pose)
  const alignment = 'capture_alignment' in raw ? poseCaptureAlignment(raw.capture_alignment) : undefined
  if (!pose || pose.parent_frame !== frame || ('capture_alignment' in raw && !alignment)) return null
  return alignment ? freeze({ kind: 'pose', pose, capture_alignment: alignment }) : freeze({ kind: 'pose', pose })
}

function rangeScanPayload(value: unknown, frame: string): RangeScanPayload | null {
  const result = exact(value, new Set(['kind', 'sensor_pose', 'angle_min_rad', 'angle_increment_rad', 'range_min_m', 'range_max_m', 'ranges_m', 'mount_id']))
  if (!result || result.kind !== 'range_scan' || !Array.isArray(result.ranges_m) || result.ranges_m.length === 0 || result.ranges_m.length > 720) return null
  const sensorPose = framedPose(result.sensor_pose)
  const angleMin = number(result.angle_min_rad)
  const angleIncrement = number(result.angle_increment_rad)
  const rangeMin = number(result.range_min_m)
  const rangeMax = number(result.range_max_m)
  const mountId = text(result.mount_id, 512)
  if (!sensorPose || sensorPose.child_frame !== frame || angleMin === null || angleIncrement === null || angleIncrement <= 0 || rangeMin === null || rangeMax === null || rangeMin < 0 || rangeMin >= rangeMax || !mountId) return null
  const ranges: (number | null)[] = []
  for (const value of result.ranges_m) {
    if (value === null) {
      ranges.push(null)
      continue
    }
    const distance = number(value)
    if (distance === null || distance < rangeMin || distance > rangeMax) return null
    ranges.push(distance)
  }
  return freeze({ kind: 'range_scan', sensor_pose: sensorPose, angle_min_rad: angleMin, angle_increment_rad: angleIncrement, range_min_m: rangeMin, range_max_m: rangeMax, ranges_m: freeze(ranges), mount_id: mountId })
}

function cameraFramePayload(value: unknown): CameraFramePayload | null {
  const result = exact(value, new Set(['kind', 'image_id', 'sha256', 'width_px', 'height_px', 'calibration_id']))
  if (!result || result.kind !== 'camera_frame') return null
  const imageId = text(result.image_id, 512)
  const sha256 = typeof result.sha256 === 'string' && /^[0-9a-f]{64}$/.test(result.sha256) ? result.sha256 : null
  const width = integer(result.width_px, 1, 16_384)
  const height = integer(result.height_px, 1, 16_384)
  const calibrationId = text(result.calibration_id, 512)
  return imageId && sha256 && width !== null && height !== null && calibrationId
    ? freeze({ kind: 'camera_frame', image_id: imageId, sha256, width_px: width, height_px: height, calibration_id: calibrationId })
    : null
}

function tagObservationPayload(value: unknown, frame: string): TagObservationPayload | null {
  const result = exact(value, new Set(['kind', 'family', 'tag_id', 'image_id', 'pose_accepted', 'tag_pose', 'covariance_m2', 'reason', 'size_m', 'corners_px', 'pixel_frame', 'reprojection_rms_px']))
  if (!result || result.kind !== 'tag_observation' || result.family !== 'tag36h11' || typeof result.pose_accepted !== 'boolean') return null
  const tagId = integer(result.tag_id, 0, 586)
  const imageId = text(result.image_id, 512)
  const reason = tagReason(result.reason)
  const pixelFrame = result.pixel_frame === 'camera' || result.pixel_frame === 'rectified_camera' ? result.pixel_frame : null
  const tagPose = result.tag_pose === null ? null : framedPose(result.tag_pose)
  const size = result.size_m === null ? null : number(result.size_m)
  const rms = result.reprojection_rms_px === null ? null : number(result.reprojection_rms_px, 16_384)
  const corners = cornersPx(result.corners_px)
  const covariance = covarianceM2(result.covariance_m2)
  if (tagId === null || !imageId || !reason || !pixelFrame || !corners || (tagPose === null && result.tag_pose !== null) || (size === null && result.size_m !== null) || (covariance === null && result.covariance_m2 !== null) || (rms === null && result.reprojection_rms_px !== null) || (rms !== null && rms < 0)) return null
  if (result.pose_accepted !== (reason === 'pose') || (tagPose !== null) !== result.pose_accepted || (tagPose && (tagPose.parent_frame !== frame || tagPose.child_frame !== `tag:${tagId}`))) return null
  if ((size === null && result.pose_accepted) || (size !== null && size <= 0) || (covariance !== null && tagPose === null)) return null
  return freeze({ kind: 'tag_observation', family: 'tag36h11', tag_id: tagId, image_id: imageId, pose_accepted: result.pose_accepted, tag_pose: tagPose, covariance_m2: covariance, reason, size_m: size, corners_px: corners, pixel_frame: pixelFrame, reprojection_rms_px: rms })
}

function tagReason(value: unknown): TagObservationReason | null {
  return typeof value === 'string' && ['pose', 'ambiguous', 'unconfigured_tag', 'duplicate_tag', 'tag_too_small', 'reprojection_or_cheirality'].includes(value)
    ? value as TagObservationReason
    : null
}

function cornersPx(value: unknown): readonly (readonly [number, number])[] | null {
  if (!Array.isArray(value) || value.length !== 4) return null
  const corners: [number, number][] = []
  for (const corner of value) {
    if (!Array.isArray(corner) || corner.length !== 2) return null
    const x = number(corner[0], 16_384)
    const y = number(corner[1], 16_384)
    if (x === null || y === null) return null
    corners.push(freeze([x, y]) as [number, number])
  }
  return freeze(corners)
}

function covarianceM2(value: unknown): readonly number[] | null {
  if (value === null) return null
  if (!Array.isArray(value) || value.length !== 9) return null
  const covariance = value.map((entry) => number(entry))
  if (covariance.some((entry) => entry === null)) return null
  const matrix = covariance as number[]
  const scale = Math.max(...matrix.map(Math.abs))
  const normalized = scale === 0 ? matrix : matrix.map((entry) => entry / scale)
  const [a, b, c, , d, e, , , f] = normalized
  const tolerance = 1e-9
  if (Math.abs(b - normalized[3]) > tolerance || Math.abs(c - normalized[6]) > tolerance || Math.abs(e - normalized[7]) > tolerance || a < -tolerance || d < -tolerance || f < -tolerance || a * d - b * b < -tolerance || a * f - c * c < -tolerance || d * f - e * e < -tolerance || a * d * f + 2 * b * c * e - a * e * e - d * c * c - f * b * b < -tolerance) return null
  return freeze(matrix)
}

function statusPayload(value: unknown): StatusPayload | null {
  const result = exact(value, new Set(['kind', 'code', 'detail', 'capabilities']))
  if (!result || result.kind !== 'status' || !Array.isArray(result.capabilities) || result.capabilities.length > 32) return null
  const code = text(result.code)
  const detail = text(result.detail, 512)
  const capabilities = result.capabilities.map((item) => text(item, 64))
  return code && detail && capabilities.every((item) => item !== null)
    ? freeze({ kind: 'status', code, detail, capabilities: freeze(capabilities as string[]) })
    : null
}

export function parseObservation(value: unknown): Observation | null {
  const result = exact(value, eventFields)
  if (!result || result.v !== 1 || result.type !== 'observation') return null
  const eventId = text(result.event_id)
  const session = text(result.session, 512)
  const deviceId = integer(result.device_id, 1, 2_147_483_647)
  const epoch = integer(result.connection_epoch, 1)
  const sourceId = text(result.source_id)
  const nodeType = observationNodeType(result.node_type)
  const frame = text(result.frame)
  const confidence = unitInterval(result.confidence)
  const capture = result.t_capture === null ? null : sourceTime(result.t_capture)
  const receipt = sourceTime(result.t_source_receipt)
  const mapping = result.clock_mapping_id === null ? null : text(result.clock_mapping_id)
  const ingest = integer(result.t_ingest)
  if (!eventId || !session || deviceId === null || epoch === null || !sourceId || !nodeType || !frame || confidence === null || (capture === null && result.t_capture !== null) || !receipt || mapping === null && result.clock_mapping_id !== null || ingest === null) return null
  if (capture && (capture.clock_id !== receipt.clock_id || capture.unit !== receipt.unit || capture.value > receipt.value)) return null
  const parsedPayload = payload(result.payload, frame)
  if (!parsedPayload || (parsedPayload.kind === 'pose' && parsedPayload.capture_alignment && !mapping)) return null
  const observation = freeze({ v: 1 as const, type: 'observation' as const, event_id: eventId, session, device_id: deviceId, connection_epoch: epoch, source_id: sourceId, node_type: nodeType, frame, confidence, t_capture: capture, t_source_receipt: receipt, clock_mapping_id: mapping, payload: parsedPayload, t_ingest: ingest })
  return new TextEncoder().encode(JSON.stringify(observation)).length <= MAX_OBSERVATION_BYTES ? observation : null
}

function freeze<T>(value: T): T {
  return Object.freeze(value)
}

function observationNodeType(value: unknown): ObservationNodeType | null {
  return value === 'aircraft' || value === 'ground' ? value : null
}
