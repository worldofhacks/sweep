import { useEffect, useRef, useState } from 'react'
import type { NavigationPoint } from '../../navigation/types'
import type { MultiviewPreview, MultiviewStatus } from '../../relay/multiview'
import type { ModuleProps } from '../types'
import './multiview.css'

export function MultiviewCapture({ controller, services, now }: Pick<ModuleProps, 'controller' | 'services' | 'now'>) {
  const { state, navigation } = controller
  const [zones, setZones] = useState<string[]>(['', ''])
  const [preview, setPreview] = useState<MultiviewPreview | null>(null)
  const [status, setStatus] = useState<MultiviewStatus | null>(null)
  const [active, setActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [time, setTime] = useState(now)
  const generation = useRef(0)
  const client = services.multiview
  const catalog = navigation.catalog
  const destinations = catalog?.destinations.filter((item) => !item.excluded && item.allowedClasses.includes('aircraft')) ?? []
  const device = state.selection.length === 1 ? state.aircraft[state.selection[0]] : undefined
  const ready = state.connection.status === 'connected' && state.armed && !state.estop && device?.device_class === 'aircraft' &&
    device.membership === 'ready' && device.selectable && ['hovering', 'airborne'].includes(device.flight_state ?? '') &&
    navigation.status === 'ready' && catalog !== null && catalog.expiresAt > time
  const identity = JSON.stringify([state.sessionId, state.selection, device?.connection_epoch, catalog?.map])
  const [reviewedIdentity, setReviewedIdentity] = useState<string | null>(null)
  const valid = ready && preview !== null && preview.expiresAt > time && reviewedIdentity === identity

  useEffect(() => {
    const timer = window.setInterval(() => setTime(now()), 500)
    return () => window.clearInterval(timer)
  }, [now])
  useEffect(() => {
    if (!active || !preview || !client) return
    let cancelled = false
    const load = async () => {
      try {
        const value = await client.status(preview)
        if (cancelled) return
        setStatus(value)
        setError(null)
        if (value.status === 'failed' || value.status === 'completed') setActive(false)
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : 'Photo-route status is unavailable.')
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 1000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [active, preview, client])

  const edit = (next: string[]) => { generation.current++; setZones(next); setPreview(null); setStatus(null); setError(null) }
  const prepare = async () => {
    if (!client || !ready || !device) return
    const requestGeneration = ++generation.current
    const requestedIdentity = identity
    setBusy(true); setError(null); setPreview(null); setStatus(null)
    try {
      const intentId = `multiview-${crypto.randomUUID()}`
      const value = await client.preview({ intentId, selected: [{ id: device.drone_id, deviceClass: 'aircraft', epoch: device.connection_epoch }],
        viewpoints: zones.map((zoneId, index) => ({ viewpointId: `view-${index + 1}`, zoneId, captureId: `${intentId}-${index + 1}` })) })
      if (requestGeneration !== generation.current) return
      if (!catalog || value.execution.approvalId !== catalog.map.approvalId ||
        (['mapPin', 'geometryPin', 'navigationPin'] as const).some((key) =>
          value.execution[key].version !== catalog.map[key].version || value.execution[key].contentSha256 !== catalog.map[key].contentSha256)) {
        throw new Error('The accepted map changed while preparing the photo route.')
      }
      setReviewedIdentity(requestedIdentity)
      setPreview(value)
    } catch (reason) {
      if (requestGeneration === generation.current) setError(reason instanceof Error ? reason.message : 'The photo route could not be prepared.')
    } finally { if (requestGeneration === generation.current) setBusy(false) }
  }
  const confirm = async () => {
    if (!client || !preview || !valid) return
    setBusy(true); setError(null)
    try { await client.confirm(preview); setActive(true) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The photo route was refused.') }
    finally { setBusy(false) }
  }

  return <section className="mv-capture" aria-label="Multiple viewpoints">
    <h3>Photograph from multiple positions</h3>
    <p>Choose approved stops in order. The aircraft holds each arrival position, takes one native photograph, and retrieves it before continuing.</p>
    {!client && <p role="status">The photo-route connection is unavailable.</p>}
    {client && <>
      <ol className="mv-stops">{zones.map((zone, index) => <li key={index}>
        <label>Stop {index + 1}<select aria-label={`Photo stop ${index + 1}`} value={zone} disabled={busy || active}
          onChange={(event) => edit(zones.map((item, slot) => slot === index ? event.target.value : item))}>
          <option value="">Choose an approved destination</option>
          {destinations.map((item) => <option key={item.zoneId} value={item.zoneId}>{item.name}</option>)}
        </select></label>
        <button type="button" disabled={zones.length <= 1 || busy || active} aria-label={`Remove photo stop ${index + 1}`}
          onClick={() => edit(zones.filter((_, slot) => slot !== index))}>Remove</button>
      </li>)}</ol>
      <div className="mv-actions">
        <button type="button" disabled={zones.length >= 8 || busy || active} onClick={() => edit([...zones, ''])}>Add stop</button>
        <button type="button" disabled={!ready || zones.some((zone) => !destinations.some((item) => item.zoneId === zone)) || busy || active}
          onClick={() => void prepare()}>{busy ? 'Preparing…' : 'Preview photo route'}</button>
      </div>
      {!ready && <p>Select one armed, airborne aircraft with current accepted-map navigation.</p>}
    </>}
    {preview && <div className="mv-review" aria-label="Photo route review">
      <PhotoRoute preview={preview} />
      <ol>{preview.views.map((view, index) => <li key={view.viewpointId}>
        <strong>{index + 1}. {destinations.find((item) => item.zoneId === view.zoneId)?.name ?? view.zoneId}</strong>
        <span>Hold at {position(view.route.arrivalSlot.position)} · one photograph</span>
      </li>)}</ol>
      <details><summary>Review route coordinates</summary>
        <table><thead><tr><th>Stop</th><th>Waypoint</th><th>X · Y · height (m)</th></tr></thead><tbody>
          {preview.views.flatMap((view, index) => view.route.waypoints.map((point, waypoint) =>
            <tr key={`${view.viewpointId}-${waypoint}`}><td>{index + 1}</td><td>{waypoint + 1}</td><td>{position(point)}</td></tr>))}
        </tbody></table>
      </details>
      {!active && !status && <button type="button" disabled={!valid || busy} onClick={() => void confirm()}>Confirm photo route</button>}
      {!valid && !active && !status && <p role="status">The review expired or its aircraft or map changed. Preview again.</p>}
    </div>}
    {status && <div role="status"><p>Photo route: {status.status}</p>
      <ol>{status.views.map((view) => <li key={view.viewpointId}>{view.zoneId}: {view.state}. {view.detail}</li>)}</ol>
    </div>}
    {active && <p>Use the console’s Hold or network stop controls to interrupt the mission.</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}

function position(point: NavigationPoint) { return `${point.xM.toFixed(2)}, ${point.yM.toFixed(2)}, ${point.zM.toFixed(2)} m` }
function PhotoRoute({ preview }: { preview: MultiviewPreview }) {
  const points = preview.views.flatMap((view) => view.route.waypoints)
  const minX = Math.min(...points.map((p) => p.xM)), minY = Math.min(...points.map((p) => p.yM))
  const scale = Math.min(520 / Math.max(1, Math.max(...points.map((p) => p.xM)) - minX), 180 / Math.max(1, Math.max(...points.map((p) => p.yM)) - minY))
  const x = (p: NavigationPoint) => 40 + (p.xM - minX) * scale
  const y = (p: NavigationPoint) => 220 - (p.yM - minY) * scale
  return <figure><svg viewBox="0 0 600 260" role="img" aria-label="Reviewed photo route viewed from above">
    {preview.views.map((view, index) => <g key={view.viewpointId}>
      <polyline points={view.route.waypoints.map((p) => `${x(p)},${y(p)}`).join(' ')} />
      <circle cx={x(view.route.arrivalSlot.position)} cy={y(view.route.arrivalSlot.position)} r="5" />
      <text x={x(view.route.arrivalSlot.position) + 9} y={y(view.route.arrivalSlot.position) - 9}>{index + 1}</text>
    </g>)}
  </svg><figcaption>Stops in capture order, viewed from above in the accepted map.</figcaption></figure>
}
