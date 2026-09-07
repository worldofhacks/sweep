import { formatDeviceId } from '../../../control/state'
import { useRef, useState } from 'react'
import type { ControlState } from '../../../control/state'
import { supports, UNAVAILABLE_MAP_AUTHORING_CLIENT, type AuthoringOperation, type MapAuthoringClient } from './client'
import { canDraw, emptyDraft, geometryIssue } from './geometry'
import { exportLocalDraft, loadLocalDraft, loadOccupancyImage } from './files'
import { useMapAuthoring } from './use-authoring'
import { useWorldObservations } from './use-observations'
import { AuthoringCanvas } from './AuthoringCanvas'
import { FeatureInspector, NumberField, TagInspector } from './Inspectors'
import type { FeatureKind, MapDraft, MapFeature, XY } from './types'
import './authoring.css'

export interface MapAuthoringProps { client?: MapAuthoringClient; now?: () => number; state?: ControlState }

export function MapAuthoring({ client = UNAVAILABLE_MAP_AUTHORING_CLIENT, now = Date.now, state }: MapAuthoringProps) {
  const editor = useMapAuthoring(client, now, state)
  const { draft } = editor
  const registration = draft.metadata.registration ?? { sourceFrame: '', transformId: '', residualM: null, thresholdM: null, evidence: '' }
  const observations = useWorldObservations(client, draft, state, now, editor.observationReference)
  const [tool, setTool] = useState<FeatureKind | 'tag' | 'inspect'>('inspect')
  const [points, setPoints] = useState<XY[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [reviewApproval, setReviewApproval] = useState(false)
  const [reading, setReading] = useState(false)
  const fileGeneration = useRef(0)
  const ready = canDraw(draft)
  const disabled = (operation: AuthoringOperation) => !supports(client, operation) || editor.busy !== null || reading
  const unavailable = client.status === 'unavailable' ? client.reason : 'This relay does not advertise the required authoring operation.'
  const edit = (next: MapDraft) => {
    fileGeneration.current += 1
    setPoints([]); setReviewApproval(false)
    editor.changed(next)
  }
  const feature = draft.features.find((f) => f.id === selected)
  const tag = draft.tags.find((t) => t.id === selected)
  const editFeature = (next: MapFeature) => edit({ ...draft, features: draft.features.map((f) => f.id === next.id ? next : f) })
  const choose = (id: string) => { setSelected(id); setTool('inspect'); setPoints([]) }
  const addPoint = (point: XY) => {
    if (!ready || tool === 'inspect') return
    if (tool === 'tag') {
      if (draft.tags.length >= 512) { editor.setNotice('A local draft is limited to 512 tags.'); return }
      const id = crypto.randomUUID()
      edit({ ...draft, tags: [...draft.tags, { id, tagId: null, family: '', sizeM: null, position: point, heightM: null, source: 'unreported', confidence: null, observations: [], usedForFlight: false, tapeVerified: false, tapeEvidence: '' }] })
      choose(id)
    } else if (points.length < 511) setPoints([...points, point])
  }
  const finish = () => {
    if (tool === 'inspect' || tool === 'tag' || draft.features.length >= 256) return
    const closed = tool !== 'corridor'
    const shape = closed && points.length > 0 ? [...points, { ...points[0] }] : points
    const error = geometryIssue(shape, closed)
    if (error) { editor.setNotice(error); return }
    const id = crypto.randomUUID()
    edit({ ...draft, features: [...draft.features, { id, kind: tool, name: '', aliases: [], points: shape, widthM: null, flightHeightM: null, heightToleranceM: null, heightEvidence: '' }] })
    choose(id)
  }
  const importFile = async (file: File, kind: 'image' | 'draft') => {
    const generation = ++fileGeneration.current
    setReading(true); setReviewApproval(false)
    try {
      const result = kind === 'image' ? { ...emptyDraft(), image: await loadOccupancyImage(file) } : await loadLocalDraft(file)
      if (generation !== fileGeneration.current) return
      editor.replace(result); setPoints([]); setSelected(null); setTool('inspect')
      editor.setNotice(kind === 'image' ? 'Real image loaded. Enter measured map metadata before drawing.' : 'Local draft imported. Imported flags cannot establish relay validation or approval.')
    } catch (error) {
      if (generation === fileGeneration.current) editor.setNotice(error instanceof Error ? error.message : 'Import failed.')
    } finally { setReading(false) }
  }
  return <section className="ma" aria-label="Map and zone authoring">
    <header className="ma-header"><div><h2>Map and zone authoring</h2><p>{editor.approval ? 'Relay-approved revision' : editor.base && !editor.dirty ? 'Saved relay revision · approval pending' : 'Local draft · not relay approved'}</p></div>
      <div className="ma-actions">
        <button type="button" disabled={!editor.canUndo || reading} onClick={() => { fileGeneration.current += 1; setPoints([]); setReviewApproval(false); editor.undo() }}>Undo edit</button>
        <button type="button" disabled={!draft.image || reading} onClick={() => { try { exportLocalDraft(draft) } catch { editor.setNotice('The draft could not be exported.') } }}>Export local draft</button>
      </div>
    </header>
    <p className="ma-boundary">The occupancy image is a drawing surface and ground-navigation prior. Flight depends on verified tags and hand-measured corridor heights. Live occupancy cannot approve static flight geometry.</p>
    <div className="ma-file-inputs">
      <label>Load actual occupancy image<input type="file" accept="image/png,image/jpeg" disabled={reading || editor.busy !== null} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void importFile(file, 'image') }} /></label>
      <label>Import local draft<input type="file" accept="application/json,.json" disabled={reading || editor.busy !== null} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void importFile(file, 'draft') }} /></label>
    </div>
    {reading && <p role="status">Reading and checking the selected file…</p>}
    <fieldset className="ma-metadata"><legend>Measured map metadata</legend>
      {(['mapVersion', 'floorId', 'frame'] as const).map((key) => <label key={key}>{({ mapVersion: 'Map version', floorId: 'Floor ID', frame: 'Coordinate frame' })[key]}<input value={draft.metadata[key]} maxLength={256} disabled={reading} onChange={(e) => edit({ ...draft, metadata: { ...draft.metadata, [key]: e.target.value } })} /></label>)}
      <NumberField label="Resolution · metres per pixel" value={draft.metadata.resolutionM} min={0} onChange={(resolutionM) => edit({ ...draft, metadata: { ...draft.metadata, resolutionM } })} />
      <NumberField label="Bottom-left origin x · m" value={draft.metadata.originXM} onChange={(originXM) => edit({ ...draft, metadata: { ...draft.metadata, originXM } })} />
      <NumberField label="Bottom-left origin y · m" value={draft.metadata.originYM} onChange={(originYM) => edit({ ...draft, metadata: { ...draft.metadata, originYM } })} />
      <label>Distance units<select value={draft.metadata.units ?? ''} onChange={(e) => edit({ ...draft, metadata: { ...draft.metadata, units: e.target.value } })}><option value="">Select measured units</option><option value="m">Metres</option></select></label>
      <label>Map created at · UTC<input type="datetime-local" step="1" value={draft.metadata.createdAt && draft.metadata.createdAt > 0 && draft.metadata.createdAt <= 8.64e15 ? new Date(draft.metadata.createdAt).toISOString().slice(0, 19) : ''} onChange={(e) => {
        const parsed = e.target.value ? Date.parse(`${e.target.value}Z`) : NaN
        edit({ ...draft, metadata: { ...draft.metadata, createdAt: Number.isFinite(parsed) ? parsed : null } })
      }} /></label>
      <label>Map creation evidence<textarea value={draft.metadata.creationEvidence ?? ''} maxLength={4096} onChange={(e) => edit({ ...draft, metadata: { ...draft.metadata, creationEvidence: e.target.value } })} /></label>
      {(['sourceFrame', 'transformId', 'evidence'] as const).map((key) => <label key={key}>{({ sourceFrame: 'Original image frame', transformId: 'Registration identity', evidence: 'Registration measurement evidence' })[key]}<input value={registration[key]} maxLength={4096} onChange={(e) => edit({ ...draft, metadata: { ...draft.metadata, registration: { ...registration, [key]: e.target.value } } })} /></label>)}
      <NumberField label="Measured registration residual · m" min={0} value={registration.residualM} onChange={(residualM) => edit({ ...draft, metadata: { ...draft.metadata, registration: { ...registration, residualM } } })} />
      <NumberField label="Accepted registration threshold · m" min={0} value={registration.thresholdM} onChange={(thresholdM) => edit({ ...draft, metadata: { ...draft.metadata, registration: { ...registration, thresholdM } } })} />
      <p>Use <code>world</code> only for an image registered to that frame. Enter its actual resolution and bottom-left origin; renaming an unknown frame does not register it.</p>
      {draft.image && <p className="ma-image-details">{draft.image.name} · {draft.image.width} × {draft.image.height} px · SHA-256 <code>{draft.image.sha256}</code></p>}
    </fieldset>
    <div className="ma-workspace"><div>
      <div className="ma-choices" role="group" aria-label="Drawing tool">
        {(['inspect', 'zone', 'corridor', 'geofence', 'no_fly', 'obstacle', 'tag'] as const).map((value) => <button type="button" key={value} aria-pressed={tool === value} disabled={!ready || reading} onClick={() => { setTool(value); setPoints([]) }}>{({ inspect: 'Inspect', zone: 'Draw zone', corridor: 'Draw corridor', geofence: 'Draw geofence', no_fly: 'Draw no-fly area', obstacle: 'Draw obstacle', tag: 'Place tag' })[value]}</button>)}
      </div>
      <div className="ma-canvas-wrap">
        {ready ? <AuthoringCanvas draft={draft} selected={selected} drawing={points} onPoint={addPoint} onSelect={choose} onEdit={editFeature} positions={observations.positions} /> : draft.image ? <img className="ma-image-preview" src={draft.image.dataUrl} alt="Operator-loaded occupancy image; coordinates not yet configured" /> : <div className="ma-empty">Load an actual occupancy image. No map or device positions are generated.</div>}
      </div>
      <p className="ma-help">{!ready ? 'Drawing is unavailable until the real image and all required map metadata are supplied.' : tool === 'inspect' ? 'Select an object to edit it. Drag a selected vertex or edit its metre coordinates.' : tool === 'tag' ? 'Click the measured tag location, then enter its printed identity and evidence.' : 'Click vertices in order, then finish. Polygons close explicitly; corridor points form an open centerline.'}</p>
      {points.length > 0 && <div className="ma-actions"><span>{points.length} points</span><button type="button" onClick={finish}>Finish geometry</button><button type="button" onClick={() => setPoints(points.slice(0, -1))}>Undo point</button><button type="button" onClick={() => setPoints([])}>Cancel drawing</button></div>}
      <p className="ma-help">{observations.reason} Legacy x/y telemetry is never recorded as a world observation.</p>
      {observations.positions.length > 0 && <ul className="ma-observations" aria-label="Verified position evidence">{observations.positions.map(({ observation, label }) => <li key={observation.deviceId}>{label} · {observation.sourceId} · confidence {observation.confidence} · epoch {observation.connectionEpoch} · observation {observation.observationId}</li>)}</ul>}
    </div><aside className="ma-sidebar">
      <div className="ma-object-list" role="group" aria-label="Draft objects">
        {draft.features.map((f) => <button type="button" key={f.id} aria-pressed={selected === f.id} onClick={() => choose(f.id)}>{f.name || `Unnamed ${f.kind.replace('_', '-')}`}</button>)}
        {draft.tags.map((t) => <button type="button" key={t.id} aria-pressed={selected === t.id} onClick={() => choose(t.id)}>Tag {t.tagId ?? 'unreported'}</button>)}
        {draft.features.length + draft.tags.length === 0 && <p>No authored objects.</p>}
      </div>
      {feature && <FeatureInspector feature={feature} onChange={editFeature} onDelete={() => { edit({ ...draft, features: draft.features.filter((f) => f.id !== feature.id) }); setSelected(null) }} />}
      {tag && <TagInspector tag={tag} onChange={(next) => edit({ ...draft, tags: draft.tags.map((t) => t.id === next.id ? next : t) })} onDelete={() => { edit({ ...draft, tags: draft.tags.filter((t) => t.id !== tag.id) }); setSelected(null) }}
        canRecord={!disabled('record') && ready && tag.tagId !== null && editor.recordTarget !== null}
        recordLabel={editor.recordTarget ? `Record at ${formatDeviceId(editor.recordTarget)} current position` : undefined} onRecord={() => { setReviewApproval(false); void editor.record(tag.id) }}
        recordReason={!editor.observationReference ? 'Load or save the exact coordinate source first. Image and registration changes require a new approved source.' : disabled('record') || !state || !['connected', 'degraded'].includes(state.connection.status) ? 'A supported relay observation service with a current session roster and frame/epoch evidence is required.' : 'The selected tag needs its printed ID and a registered world-frame map.'} />}
    </aside></div>
    <details className="ma-checks" open={editor.issues.length > 0}><summary>Local checks · {editor.issues.length ? `${editor.issues.length} issues` : 'passed'}</summary>
      <p>Local checks help edit a draft. Server validation and measurement evidence are required for approval.</p>
      <ul>{editor.issues.map((issue, index) => <li key={`${issue.path}-${index}`}><code>{issue.path}</code> · {issue.message}</li>)}</ul>
    </details>
    <section className="ma-relay" aria-label="Relay map workflow"><h3>Relay versions and approval</h3>
      {client.status === 'unavailable' && <p>{client.reason}</p>}
      <div className="ma-actions">
        <button type="button" disabled={disabled('list')} title={disabled('list') ? unavailable : undefined} onClick={() => void editor.list()}>Load revision list</button>
        <button type="button" disabled={disabled('save') || editor.issues.length > 0} title={disabled('save') ? unavailable : 'Save requires passing local checks.'} onClick={() => { setReviewApproval(false); void editor.save() }}>Save to relay</button>
        <button type="button" disabled={disabled('validate') || !editor.base || editor.dirty || editor.issues.length > 0} title={disabled('validate') ? unavailable : 'Save the unchanged draft first.'} onClick={() => { setReviewApproval(false); void editor.validate() }}>Validate saved revision</button>
        <button type="button" disabled={disabled('approve') || !editor.canApprove || editor.approval !== null} title={disabled('approve') ? unavailable : 'Requires passing server validation of this exact revision.'} onClick={() => setReviewApproval(true)}>Review approval</button>
        <button type="button" disabled={disabled('activate') || !editor.base || editor.dirty} title="Selects only an already approved revision. No motion is requested." onClick={() => { setReviewApproval(false); void editor.activate() }}>Use approved revision for navigation</button>
      </div>
      {editor.base && <p>Saved identity: <code>{editor.base.bundleId} / {editor.base.revision}</code> · <code>{editor.base.contentHash}</code>{editor.dirty ? ' · local changes pending' : ''}</p>}
      {editor.validation && <div><p>Server validation {editor.validation.valid && editor.validation.issues.length === 0 ? 'passed' : 'refused'} · {editor.validation.validationId}</p><ul>{editor.validation.issues.map((issue, index) => <li key={index}>{issue.path} · {issue.message}</li>)}</ul></div>}
      {reviewApproval && editor.canApprove && editor.base && editor.validation && <div className="ma-approval" role="group" aria-label="Confirm exact map approval">
        <p>Approve only <strong>{editor.base.bundleId} / {editor.base.revision}</strong>, hash <code>{editor.base.contentHash}</code>, validation <code>{editor.validation.validationId}</code>. This requests an audited relay decision.</p>
        <button type="button" disabled={editor.busy !== null} onClick={() => { setReviewApproval(false); void editor.approve() }}>Approve exact saved revision</button>
        <button type="button" onClick={() => setReviewApproval(false)}>Cancel approval</button>
      </div>}
      {editor.approval && <p>Approved by {editor.approval.approvedBy} · audit <code>{editor.approval.auditId}</code> · {new Date(editor.approval.approvedAt).toISOString()}</p>}
      {editor.revisions.length > 0 && <ul className="ma-revisions">{editor.revisions.map((r) => <li key={`${r.bundleId}:${r.revision}:${r.contentHash}`}><span>{r.label} · {r.revision}</span>
        <button type="button" disabled={disabled('load')} onClick={() => { setReviewApproval(false); setPoints([]); setSelected(null); void editor.load(r) }}>Load {r.revision}</button>
        <button type="button" disabled={disabled('compare') || !editor.base || editor.dirty} onClick={() => void editor.compare(r)}>Compare with {r.revision}</button>
      </li>)}</ul>}
      {editor.comparison && <div className="ma-comparison"><p>Relay comparison: {editor.comparison.left.revision} → {editor.comparison.right.revision}</p><table><thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead><tbody>{editor.comparison.changes.map((change, index) => <tr key={index}><td>{change.path}</td><td>{change.before}</td><td>{change.after}</td></tr>)}</tbody></table></div>}
    </section>
    <p role="status" className="ma-notice">{editor.busy ? `${editor.busy} request pending… ` : ''}{editor.notice}</p>
  </section>
}
