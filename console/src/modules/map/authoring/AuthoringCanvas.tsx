import { useState, type PointerEvent } from 'react'
import { imageCoordinates, worldCoordinates } from './geometry'
import type { MapDraft, MapFeature, WorldPositionObservation, XY } from './types'

export function AuthoringCanvas({ draft, selected, drawing, onPoint, onSelect, onEdit, positions = [] }: {
  draft: MapDraft; selected: string | null; drawing: XY[]; onPoint: (point: XY) => void;
  onSelect: (id: string) => void; onEdit: (feature: MapFeature) => void
  positions?: Array<{ observation: WorldPositionObservation; label: string }>
}) {
  const [drag, setDrag] = useState<{ id: string; index: number; points: XY[]; pointerId: number } | null>(null)
  if (!draft.image) return <div className="ma-empty">Load an actual occupancy image to start drawing. No map or device positions are generated.</div>
  const { width, height } = draft.image
  const pointAt = (event: PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    return worldCoordinates(draft, { x: Math.max(0, Math.min(width, (event.clientX - bounds.left) * width / bounds.width)), y: Math.max(0, Math.min(height, (event.clientY - bounds.top) * height / bounds.height)) })
  }
  const pixels = (points: XY[]) => points.map((p) => { const pixel = imageCoordinates(draft, p); return `${pixel.x},${pixel.y}` }).join(' ')
  const marker = Math.max(width, height) / 100
  return <svg className="ma-canvas" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Map authoring canvas" preserveAspectRatio="none"
    onPointerDown={(event) => { if (event.button === 0 && !drag) onPoint(pointAt(event)) }}
    onPointerMove={(event) => {
      if (!drag || drag.pointerId !== event.pointerId) return
      const feature = draft.features.find((f) => f.id === drag.id)
      if (!feature) return
      const next = drag.points.map((p, i) => i === drag.index ? pointAt(event) : p)
      if (feature.kind !== 'corridor' && drag.index === 0) next[next.length - 1] = { ...next[0] }
      setDrag({ ...drag, points: next })
    }}
    onPointerUp={(event) => {
      if (!drag || drag.pointerId !== event.pointerId) return
      const feature = draft.features.find((f) => f.id === drag.id)
      if (feature) onEdit({ ...feature, points: drag.points })
      setDrag(null)
      event.currentTarget.releasePointerCapture?.(event.pointerId)
    }} onPointerCancel={() => setDrag(null)}>
    <image href={draft.image.dataUrl} width={width} height={height} />
    {draft.features.map((feature) => {
      const points = drag?.id === feature.id ? drag.points : feature.points
      const corridor = feature.kind === 'corridor'
      return <g key={feature.id} className={`ma-shape ma-${feature.kind} ${selected === feature.id ? 'is-selected' : ''}`}
        role="button" tabIndex={0} aria-label={`Select ${feature.name || feature.kind}`} onPointerDown={(event) => { event.stopPropagation(); onSelect(feature.id) }}
        onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(feature.id) } }}>
        {corridor && feature.widthM !== null && feature.widthM > 0 && <polyline className="ma-corridor-width" points={pixels(points)} style={{ strokeWidth: feature.widthM / draft.metadata.resolutionM! }} />}
        <polyline points={pixels(points)} />
        {selected === feature.id && (corridor ? points : points.slice(0, -1)).map((point, index) => {
          const p = imageCoordinates(draft, point)
          return <circle key={index} cx={p.x} cy={p.y} r={marker / 2} className="ma-vertex" onPointerDown={(event) => {
            event.stopPropagation()
            setDrag({ id: feature.id, index, points: feature.points.map((p) => ({ ...p })), pointerId: event.pointerId })
            event.currentTarget.ownerSVGElement?.setPointerCapture?.(event.pointerId)
          }} />
        })}
      </g>
    })}
    {draft.tags.map((tag) => {
      const p = imageCoordinates(draft, tag.position)
      return <g key={tag.id} className={`ma-tag ${selected === tag.id ? 'is-selected' : ''}`} role="button" tabIndex={0} aria-label={`Select tag ${tag.tagId ?? 'unreported'}`}
        onPointerDown={(event) => { event.stopPropagation(); onSelect(tag.id) }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(tag.id) } }}>
        <rect x={p.x - marker / 2} y={p.y - marker / 2} width={marker} height={marker} />
        <text x={p.x + marker} y={p.y} style={{ fontSize: marker }}>{tag.tagId ?? '?'}</text>
      </g>
    })}
    {drawing.length > 0 && <g className="ma-drawing"><polyline points={pixels(drawing)} />{drawing.map((point, index) => { const p = imageCoordinates(draft, point); return <circle key={index} cx={p.x} cy={p.y} r={marker / 3} /> })}</g>}
    {positions.map(({ observation, label }) => { const p = imageCoordinates(draft, observation.position); return <g className="ma-live-position" key={observation.deviceId} aria-label={`${label} verified position`}>
      <circle cx={p.x} cy={p.y} r={marker * 0.7} /><text x={p.x + marker} y={p.y - marker} style={{ fontSize: marker }}>{label}</text>
    </g> })}
  </svg>
}
