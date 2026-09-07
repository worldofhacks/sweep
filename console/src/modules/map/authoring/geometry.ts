import type { DraftIssue, MapDraft, XY } from './types'

export const emptyDraft = (): MapDraft => ({
  format: 'sweep-map-draft-v1',
  metadata: { mapVersion: '', floorId: '', frame: '', resolutionM: null, originXM: null, originYM: null },
  image: null, features: [], tags: [],
})

/** Evidence belongs to the measured identity and geometry, not the editor row ID. */
export function invalidateChangedEvidence(previous: MapDraft, next: MapDraft, recordedTag?: string): MapDraft {
  const mapChanged = JSON.stringify(previous.metadata) !== JSON.stringify(next.metadata)
    || previous.image?.sha256 !== next.image?.sha256
  return {
    ...next,
    tags: next.tags.map((tag) => {
      const before = previous.tags.find((t) => t.id === tag.id)
      if (!before) return tag
      const changed = mapChanged || before.tagId !== tag.tagId || before.family !== tag.family || before.sizeM !== tag.sizeM || before.heightM !== tag.heightM
        || before.position.x !== tag.position.x || before.position.y !== tag.position.y || before.source !== tag.source
      return changed ? { ...tag, tapeVerified: false, tapeEvidence: '', observations: !mapChanged && tag.id === recordedTag ? tag.observations : [] } : tag
    }),
    features: next.features.map((feature) => {
      const before = previous.features.find((f) => f.id === feature.id)
      const changed = before && (mapChanged || JSON.stringify(before.points) !== JSON.stringify(feature.points)
        || before.widthM !== feature.widthM || before.flightHeightM !== feature.flightHeightM || before.heightToleranceM !== feature.heightToleranceM)
      return changed && feature.kind === 'corridor' ? { ...feature, heightEvidence: '' } : feature
    }),
  }
}

const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n)
const cross = (a: XY, b: XY, c: XY) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
const equal = (a: XY, b: XY) => a.x === b.x && a.y === b.y
const onSegment = (a: XY, b: XY, p: XY) => Math.abs(cross(a, b, p)) < 1e-9
  && p.x >= Math.min(a.x, b.x) - 1e-9 && p.x <= Math.max(a.x, b.x) + 1e-9
  && p.y >= Math.min(a.y, b.y) - 1e-9 && p.y <= Math.max(a.y, b.y) + 1e-9

function intersects(a: XY, b: XY, c: XY, d: XY): boolean {
  return (cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0)
    || onSegment(a, b, c) || onSegment(a, b, d) || onSegment(c, d, a) || onSegment(c, d, b)
}

export function geometryIssue(points: XY[], closed: boolean): string | null {
  if (points.some((p) => !finite(p.x) || !finite(p.y) || Math.abs(p.x) > 1_000_000 || Math.abs(p.y) > 1_000_000)) return 'Coordinates must be finite metres within the local editor bounds.'
  if (points.length < (closed ? 4 : 2)) return closed ? 'A polygon needs three vertices and closure.' : 'A corridor needs two points.'
  if (closed && !equal(points[0], points.at(-1)!)) return 'Polygon is not closed.'
  const count = points.length - 1
  if (points.slice(1).some((p, i) => equal(p, points[i]))) return 'Geometry has a zero-length edge.'
  for (let i = 0; i < count; i += 1) {
    for (let j = i + 2; j < count; j += 1) {
      if (closed && i === 0 && j === count - 1) continue
      if (intersects(points[i], points[i + 1], points[j], points[j + 1])) return 'Geometry crosses or touches itself.'
    }
  }
  if (closed && Math.abs(points.slice(1).reduce((area, p, i) => area + points[i].x * p.y - p.x * points[i].y, 0)) < 1e-9) return 'Polygon has zero area.'
  return null
}

function inside(point: XY, polygon: XY[]): boolean {
  let result = false
  for (let i = 0; i < polygon.length - 1; i += 1) {
    const a = polygon[i], b = polygon[i + 1]
    if (onSegment(a, b, point)) return true
    if ((a.y > point.y) !== (b.y > point.y) && point.x < a.x + (point.y - a.y) * (b.x - a.x) / (b.y - a.y)) result = !result
  }
  return result
}

function pointDistance(p: XY, a: XY, b: XY): number {
  const length = (b.x - a.x) ** 2 + (b.y - a.y) ** 2
  const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / length))
  return Math.hypot(p.x - a.x - t * (b.x - a.x), p.y - a.y - t * (b.y - a.y))
}

function within(points: XY[], polygon: XY[], margin = 0): boolean {
  if (points.some((p) => !inside(p, polygon))) return false
  for (let i = 0; i < points.length - 1; i += 1) {
    const a = points[i], b = points[i + 1]
    if (!inside({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, polygon)) return false
    for (let j = 0; j < polygon.length - 1; j += 1) {
      const c = polygon[j], d = polygon[j + 1]
      if (cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0) return false
      if (margin > 0 && Math.min(pointDistance(a, c, d), pointDistance(b, c, d), pointDistance(c, a, b), pointDistance(d, a, b)) + 1e-9 < margin) return false
    }
  }
  return true
}

export function validateDraft(draft: MapDraft): DraftIssue[] {
  const issues: DraftIssue[] = []
  const add = (path: string, message: string) => issues.push({ path, message })
  const m = draft.metadata
  if (!draft.image) add('image', 'Load a real occupancy image.')
  if (!m.mapVersion.trim()) add('metadata.mapVersion', 'Map version is required.')
  if (!m.floorId.trim()) add('metadata.floorId', 'Floor identifier is required.')
  if (m.frame !== 'world') add('metadata.frame', 'An explicit canonical world frame is required; do not rename an unregistered frame.')
  if (!finite(m.resolutionM) || m.resolutionM <= 0) add('metadata.resolutionM', 'Measured image resolution must be positive metres per pixel.')
  if (!finite(m.originXM) || !finite(m.originYM)) add('metadata.origin', 'The measured bottom-left image origin is required.')
  if (draft.features.filter((f) => f.kind === 'geofence').length !== 1) add('geofence', 'Exactly one geofence is required.')
  const names = new Set<string>()
  const ids = new Set<string>()
  const geofence = draft.features.find((f) => f.kind === 'geofence')
  const boundary = geofence && geometryIssue(geofence.points, true) === null ? geofence.points : null
  for (const f of draft.features) {
    const path = `features.${f.id}`
    if (ids.has(f.id)) add(path, 'Duplicate feature ID.')
    ids.add(f.id)
    if (!f.name.trim()) add(path, 'A feature name is required.')
    for (const label of [f.name, ...f.aliases]) {
      const key = label.trim().toLocaleLowerCase()
      if (key && names.has(key)) add(path, 'Names and aliases must be unique.')
      if (key) names.add(key)
    }
    const issue = geometryIssue(f.points, f.kind !== 'corridor')
    if (issue) add(path, issue)
    if (!issue && boundary && f.kind !== 'geofence' && !within(f.points, boundary, f.kind === 'corridor' && finite(f.widthM) ? f.widthM / 2 : 0)) add(path, 'Geometry, including corridor width, must remain inside the geofence.')
    if (f.kind === 'corridor') {
      if (!finite(f.widthM) || f.widthM <= 0) add(path, 'Corridor width must be positive.')
      if (!finite(f.flightHeightM) || f.flightHeightM <= 0 || !finite(f.heightToleranceM) || f.heightToleranceM <= 0) add(path, 'Enter a hand-measured flight height and positive tolerance.')
      if (!f.heightEvidence.trim()) add(path, 'Hand-measured flight-height evidence is required; LiDAR-plane height is insufficient.')
    }
  }
  const tagIds = new Set<number>()
  for (const t of draft.tags) {
    const path = `tags.${t.id}`
    if (ids.has(t.id)) add(path, 'Duplicate object ID.')
    ids.add(t.id)
    if (t.tagId === null || !Number.isSafeInteger(t.tagId) || t.tagId < 0) add(path, 'A nonnegative safe-integer tag ID is required.')
    else if (tagIds.has(t.tagId)) add(path, 'Duplicate tag ID.')
    else tagIds.add(t.tagId)
    if (!t.family.trim() || !finite(t.sizeM) || t.sizeM <= 0 || !finite(t.heightM)) add(path, 'Tag family, measured size, and height are required.')
    if (!finite(t.position.x) || !finite(t.position.y) || (boundary && !inside(t.position, boundary))) add(path, 'Tag position must be finite and inside the geofence.')
    if (t.source === 'unreported' || !finite(t.confidence) || t.confidence < 0 || t.confidence > 1) add(path, 'Report tag provenance and confidence from 0 to 1.')
    if (t.usedForFlight && (!t.tapeVerified || !t.tapeEvidence.trim() || t.observations.length === 0)) add(path, 'Flight tags require tape verification, evidence, and observation references.')
  }
  return issues
}

export function imageCoordinates(draft: MapDraft, point: XY): XY {
  const m = draft.metadata
  return { x: (point.x - m.originXM!) / m.resolutionM!, y: draft.image!.height - (point.y - m.originYM!) / m.resolutionM! }
}

export function worldCoordinates(draft: MapDraft, point: XY): XY {
  const m = draft.metadata
  return { x: m.originXM! + point.x * m.resolutionM!, y: m.originYM! + (draft.image!.height - point.y) * m.resolutionM! }
}

export function canDraw(draft: MapDraft): boolean {
  const m = draft.metadata
  return draft.image !== null && m.frame === 'world' && Boolean(m.mapVersion.trim()) && Boolean(m.floorId.trim())
    && finite(m.resolutionM) && m.resolutionM > 0 && finite(m.originXM) && finite(m.originYM)
}
