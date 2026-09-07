import type { MapFeature, MapTag, TagSource } from './types'

export function NumberField({ label, value, onChange, min, max }: { label: string; value: number | null; onChange: (value: number | null) => void; min?: number; max?: number }) {
  return <label>{label}<input type="number" step="any" min={min} max={max} value={value ?? ''}
    onChange={(event) => onChange(Number.isFinite(event.target.valueAsNumber) ? event.target.valueAsNumber : null)} /></label>
}

export function FeatureInspector({ feature, onChange, onDelete }: { feature: MapFeature; onChange: (feature: MapFeature) => void; onDelete: () => void }) {
  const closed = feature.kind !== 'corridor'
  const points = closed ? feature.points.slice(0, -1) : feature.points
  return <fieldset className="ma-inspector"><legend>Selected {feature.kind.replace('_', '-')}</legend>
    <label>Name<input value={feature.name} maxLength={256} onChange={(e) => onChange({ ...feature, name: e.target.value })} /></label>
    <label>Aliases · comma separated<input value={feature.aliases.join(', ')} maxLength={1024} onChange={(e) => onChange({ ...feature, aliases: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} /></label>
    {feature.kind === 'corridor' && <>
      <NumberField label="Corridor width · m" min={0} value={feature.widthM} onChange={(widthM) => onChange({ ...feature, widthM })} />
      <NumberField label="Hand-measured flight height · m" min={0} value={feature.flightHeightM} onChange={(flightHeightM) => onChange({ ...feature, flightHeightM })} />
      <NumberField label="Flight height tolerance · m" min={0} value={feature.heightToleranceM} onChange={(heightToleranceM) => onChange({ ...feature, heightToleranceM })} />
      <label>Height measurement evidence<textarea value={feature.heightEvidence} maxLength={4096} onChange={(e) => onChange({ ...feature, heightEvidence: e.target.value })} /></label>
      <p>Enter measurements taken at flight height. The occupancy image and LiDAR plane cannot supply them.</p>
    </>}
    <details><summary>Vertex coordinates · metres</summary>
      {points.map((point, index) => <div className="ma-coordinate" key={index}>
        {(['x', 'y'] as const).map((axis) => <NumberField key={axis} label={`Vertex ${index + 1} ${axis}`} value={point[axis]} onChange={(value) => {
          if (value === null || !Number.isFinite(value)) return
          const next = points.map((p, i) => i === index ? { ...p, [axis]: value } : p)
          onChange({ ...feature, points: closed ? [...next, { ...next[0] }] : next })
        }} />)}
      </div>)}
    </details>
    <button type="button" className="ma-danger" onClick={onDelete}>Delete feature</button>
  </fieldset>
}

export function TagInspector({ tag, onChange, onDelete, canRecord, onRecord, recordReason, recordLabel }: {
  tag: MapTag; onChange: (tag: MapTag) => void; onDelete: () => void; canRecord: boolean; onRecord: () => void; recordReason: string; recordLabel?: string
}) {
  return <fieldset className="ma-inspector"><legend>Selected tag</legend>
    <NumberField label="Printed tag ID" min={0} value={tag.tagId} onChange={(tagId) => onChange({ ...tag, tagId })} />
    <label>Tag family<input value={tag.family} maxLength={256} onChange={(e) => onChange({ ...tag, family: e.target.value })} /></label>
    <NumberField label="Measured tag size · m" min={0} value={tag.sizeM} onChange={(sizeM) => onChange({ ...tag, sizeM })} />
    <NumberField label="Tag height · m" value={tag.heightM} onChange={(heightM) => onChange({ ...tag, heightM })} />
    <div className="ma-coordinate">{(['x', 'y'] as const).map((axis) => <NumberField key={axis} label={`Tag ${axis} · m`} value={tag.position[axis]} onChange={(value) => {
      if (value !== null && Number.isFinite(value)) onChange({ ...tag, position: { ...tag.position, [axis]: value }, tapeVerified: false, tapeEvidence: '' })
    }} />)}</div>
    <div role="group" aria-label="Tag source" className="ma-choices">{(['unreported', 'measured', 'surveyed', 'auto_registered'] as TagSource[]).map((source) => <button type="button" key={source} aria-pressed={tag.source === source} onClick={() => onChange({ ...tag, source })}>{source.replace('_', ' ')}</button>)}</div>
    <NumberField label="Tag confidence · 0 to 1" min={0} max={1} value={tag.confidence} onChange={(confidence) => onChange({ ...tag, confidence })} />
    <label>Observation references · one per line<textarea value={tag.observations.join('\n')} maxLength={4096} onChange={(e) => onChange({ ...tag, observations: e.target.value.split('\n').map((v) => v.trim()).filter(Boolean) })} /></label>
    <label className="ma-check"><input type="checkbox" checked={tag.usedForFlight} onChange={(e) => onChange({ ...tag, usedForFlight: e.target.checked })} />Use this tag for flight</label>
    <label className="ma-check"><input type="checkbox" checked={tag.tapeVerified} onChange={(e) => onChange({ ...tag, tapeVerified: e.target.checked })} />Tape verification recorded</label>
    <label>Tape measurement evidence<textarea value={tag.tapeEvidence} maxLength={4096} onChange={(e) => onChange({ ...tag, tapeEvidence: e.target.value })} /></label>
    <p>These are draft evidence claims. Only the relay can validate and approve a saved revision.</p>
    <button type="button" disabled={!canRecord} title={recordReason} onClick={onRecord}>{recordLabel ?? 'Record at current position'}</button>
    {!canRecord && <p>{recordReason}</p>}
    <button type="button" className="ma-danger" onClick={onDelete}>Delete tag</button>
  </fieldset>
}
