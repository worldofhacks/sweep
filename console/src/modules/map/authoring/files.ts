import type { MapDraft, MapFeature, MapTag, OccupancyImage, XY } from './types'

export const MAX_IMAGE_BYTES = 8 * 1024 * 1024
const MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
const fail = (): never => { throw new Error('Invalid or oversized local map draft.') }
const object = (v: unknown): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : fail()
const text = (v: unknown): string => typeof v === 'string' && v.length <= 4096 ? v : fail()
const number = (v: unknown): number => typeof v === 'number' && Number.isFinite(v) ? v : fail()
const nullable = (v: unknown): number | null => v === null ? null : number(v)
const bool = (v: unknown): boolean => typeof v === 'boolean' ? v : fail()
const array = (v: unknown, max = 512): unknown[] => Array.isArray(v) && v.length <= max ? v : fail()
const strings = (v: unknown) => array(v).map(text)
const point = (v: unknown): XY => { const p = object(v); return { x: number(p.x), y: number(p.y) } }

/** Reconstruct known draft fields; imported approval/validation flags have no authority. */
export function parseLocalDraft(raw: string): MapDraft {
  if (raw.length > MAX_DOCUMENT_BYTES) fail()
  const value = object(JSON.parse(raw))
  if (value.format !== 'sweep-map-draft-v1') fail()
  const m = object(value.metadata)
  let image: OccupancyImage | null = null
  if (value.image !== null) {
    const i = object(value.image)
    if (typeof i.dataUrl !== 'string' || i.dataUrl.length > MAX_IMAGE_BYTES * 1.4 || !/^data:image\/(png|jpeg);base64,[a-zA-Z0-9+/]+=*$/.test(i.dataUrl)) fail()
    const width = number(i.width), height = number(i.height)
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 16_777_216 || !/^[a-f0-9]{64}$/.test(text(i.sha256))) fail()
    image = { name: text(i.name), dataUrl: i.dataUrl, width, height, sha256: text(i.sha256) }
  }
  return {
    format: 'sweep-map-draft-v1',
    metadata: { mapVersion: text(m.mapVersion), floorId: text(m.floorId), frame: text(m.frame), resolutionM: nullable(m.resolutionM), originXM: nullable(m.originXM), originYM: nullable(m.originYM) },
    image,
    features: array(value.features, 256).map((raw): MapFeature => {
      const f = object(raw)
      if (f.kind !== 'zone' && f.kind !== 'geofence' && f.kind !== 'no_fly' && f.kind !== 'corridor') fail()
      return { id: text(f.id), kind: f.kind, name: text(f.name), aliases: strings(f.aliases), points: array(f.points).map(point), widthM: nullable(f.widthM), flightHeightM: nullable(f.flightHeightM), heightToleranceM: nullable(f.heightToleranceM), heightEvidence: text(f.heightEvidence) }
    }),
    tags: array(value.tags).map((raw): MapTag => {
      const t = object(raw)
      if (!['unreported', 'measured', 'surveyed', 'auto_registered'].includes(text(t.source))) fail()
      return { id: text(t.id), tagId: nullable(t.tagId), family: text(t.family), sizeM: nullable(t.sizeM), position: point(t.position), heightM: nullable(t.heightM), source: t.source as MapTag['source'], confidence: nullable(t.confidence), observations: strings(t.observations), usedForFlight: bool(t.usedForFlight), tapeVerified: bool(t.tapeVerified), tapeEvidence: text(t.tapeEvidence) }
    }),
  }
}

function readFile(file: Blob, mode: 'text' | 'data'): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('The selected file could not be read.'))
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('The selected file is not readable.'))
    if (mode === 'data') reader.readAsDataURL(file)
    else reader.readAsText(file)
  })
}

async function imageDigest(dataUrl: string): Promise<string> {
  const encoded = dataUrl.split(',')[1]
  const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0))
  if (bytes.byteLength > MAX_IMAGE_BYTES) throw new Error('Occupancy images are limited to 8 MiB.')
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (b) => b.toString(16).padStart(2, '0')).join('')
}

function dimensions(dataUrl: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onerror = () => reject(new Error('The selected occupancy image could not be decoded.'))
    image.onload = () => {
      if (image.naturalWidth < 1 || image.naturalHeight < 1 || image.naturalWidth * image.naturalHeight > 16_777_216) reject(new Error('Occupancy images are limited to 16 megapixels.'))
      else resolve({ width: image.naturalWidth, height: image.naturalHeight })
    }
    image.src = dataUrl
  })
}

export async function loadOccupancyImage(file: File): Promise<OccupancyImage> {
  if (file.size > MAX_IMAGE_BYTES || !['image/png', 'image/jpeg'].includes(file.type)) throw new Error('Choose a PNG or JPEG occupancy image no larger than 8 MiB.')
  const dataUrl = await readFile(file, 'data')
  const [size, sha256] = await Promise.all([dimensions(dataUrl), imageDigest(dataUrl)])
  return { name: file.name, dataUrl, ...size, sha256 }
}

export async function loadLocalDraft(file: File): Promise<MapDraft> {
  if (file.size > MAX_DOCUMENT_BYTES) fail()
  const draft = parseLocalDraft(await readFile(file, 'text'))
  if (draft.image) {
    const [size, digest] = await Promise.all([dimensions(draft.image.dataUrl), imageDigest(draft.image.dataUrl)])
    if (size.width !== draft.image.width || size.height !== draft.image.height || digest !== draft.image.sha256) throw new Error('Imported image dimensions or hash do not match its bytes.')
  }
  return draft
}

export function exportLocalDraft(draft: MapDraft): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'sweep-map-local-draft.json'
  anchor.click()
  URL.revokeObjectURL(url)
}
