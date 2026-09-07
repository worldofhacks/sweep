import type { DraftIssue, MapDraft, XY } from './types'

export const emptyDraft = (): MapDraft => ({
  format: 'sweep-map-draft-v1',
  metadata: { mapVersion: '', floorId: '', frame: '', resolutionM: null, originXM: null, originYM: null },
  image: null, features: [], tags: [],
})

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
  if (points.some((p) => !finite(p.x) || !finite(p.y))) return 'Coordinates must be finite metres.'
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
    if (t.tagId === null || !Number.isInteger(t.tagId) || t.tagId < 0) add(path, 'A nonnegative tag ID is required.')
    else if (tagIds.has(t.tagId)) add(path, 'Duplicate tag ID.')
    else tagIds.add(t.tagId)
    if (!t.family.trim() || !finite(t.sizeM) || t.sizeM <= 0 || !finite(t.heightM)) add(path, 'Tag family, measured size, and height are required.')
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
