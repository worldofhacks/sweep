import { useEffect, useMemo, useState } from 'react'
import type { SearchCatalog, SearchPreview, SearchStatus } from '../../search/client'
import './search.css'
import type { ModuleProps } from '../types'

const POLL_MS = 2_000

export function SearchModule({ controller, services }: ModuleProps) {
  const { state, pendingRequest, prepareSearch, cancelRequest } = controller
  const [catalog, setCatalog] = useState<SearchCatalog | null>(null)
  const [query, setQuery] = useState('')
  const [interpretation, setInterpretation] = useState<string | null>(null)
  const [zoneId, setZoneId] = useState('')
  const [targetClass, setTargetClass] = useState('')
  const [mode, setMode] = useState<'search' | 'survey'>('search')
  const [preview, setPreview] = useState<SearchPreview | null>(null)
  const [status, setStatus] = useState<SearchStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [acknowledging, setAcknowledging] = useState<string | null>(null)
  const enabled = state.enabledIntentNames.includes('search')
  const searchRequest = useMemo(
    () => state.requests.find((request) => request.intent.name === 'search' && request.status !== 'cancelled') ?? null,
    [state.requests],
  )
  const searchIntentId = searchRequest?.intent.intent_id ?? null

  useEffect(() => {
    if (!services.search || !enabled) return
    let cancelled = false
    services.search.catalog(state.sessionId).then((value) => {
      if (!cancelled) {
        setCatalog(value)
        setZoneId((current) => current || value.zones[0] || '')
        setTargetClass((current) => current || value.target_classes[0] || '')
        setError(null)
      }
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'Search configuration could not be loaded.')
    })
    return () => { cancelled = true }
  }, [services.search, state.sessionId, state.connection, enabled])

  useEffect(() => {
    if (!services.search || searchIntentId === null) return
    let cancelled = false
    const load = () => {
      void services.search!.status(state.sessionId, searchIntentId).then((value) => {
        if (!cancelled) setStatus(value)
      }).catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : 'Search status could not be loaded.')
      })
    }
    load()
    const interval = window.setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [services.search, state.sessionId, searchIntentId])

  const ready = state.connection.status === 'connected' && state.selection.length > 0 &&
    state.selection.every((id) => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable)
  const prepare = async () => {
    setBusy(true)
    setError(null)
    try {
      const prepared = await prepareSearch(zoneId, mode === 'survey' ? undefined : targetClass)
      setPreview(prepared.preview)
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The search could not be prepared.') }
    finally { setBusy(false) }
  }
  const prepareQuery = async () => {
    if (!services.search?.resolve || !catalog) return
    setBusy(true)
    setError(null)
    try {
      const resolved = await services.search.resolve(state.sessionId, query)
      setInterpretation(resolved.detail)
      if (resolved.status !== 'resolved') return
      if (!resolved.zone_id || !catalog.zones.includes(resolved.zone_id) ||
        (resolved.mode === 'search' && (!resolved.target_class || !catalog.target_classes.includes(resolved.target_class)))) {
        throw new Error('Search configuration changed. Reload the configured rooms and targets.')
      }
      setZoneId(resolved.zone_id)
      setMode(resolved.mode)
      if (resolved.mode === 'search') setTargetClass(resolved.target_class!)
      const prepared = await prepareSearch(resolved.zone_id, resolved.mode === 'survey' ? undefined : resolved.target_class!)
      setPreview(prepared.preview)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The search request could not be prepared.')
    } finally { setBusy(false) }
  }
  const acknowledge = async (sightingId: string) => {
    if (!services.search || searchRequest === null) return
    setAcknowledging(sightingId)
    try {
      setStatus(await services.search.acknowledge(state.sessionId, searchRequest.intent.intent_id, sightingId))
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The finding could not be acknowledged.')
    } finally { setAcknowledging(null) }
  }

  return (
    <section className="se-module" aria-label="Visual search">
      <div className="se-panel">
        <h2>Search configuration</h2>
        <p>Select a configured room and either search for a class or survey camera-evidenced grid coverage. The relay freezes the route and coverage tasks before confirmation.</p>
        {!enabled && <p role="status">Search is not configured on this relay.</p>}
        {enabled && !services.search && <p role="status">The search connection is unavailable.</p>}
        {services.search?.resolve && <form className="se-query" onSubmit={(event) => { event.preventDefault(); void prepareQuery() }}>
          <label htmlFor="search-query">Describe the search</label>
          <div><input id="search-query" value={query} disabled={busy || !enabled}
            placeholder="Find a backpack in the lobby" maxLength={2000}
            onChange={(event) => {
              setQuery(event.target.value)
              setInterpretation(null)
              setPreview(null)
              if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
            }} />
            <button type="submit" className="se-button" disabled={busy || !ready || !catalog || !query.trim() || !enabled}>Preview request</button>
          </div>
          <p>Use “find a backpack in the lobby” or “survey the lobby grid”. Object search supports configured classes only.</p>
          {interpretation && <p role="status">{interpretation}</p>}
        </form>}
        <div className="se-controls">
          <label>Room
            <select aria-label="Search room" value={zoneId} disabled={busy || !catalog || !enabled}
              onChange={(event) => {
                setZoneId(event.target.value)
                setPreview(null)
                setStatus(null)
                if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
              }}>
              <option value="">Choose a room</option>
              {catalog?.zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
            </select>
          </label>
          <label>Mode
            <select aria-label="Visual coverage mode" value={mode} disabled={busy || !catalog || !enabled}
              onChange={(event) => {
                setMode(event.target.value as 'search' | 'survey')
                setPreview(null)
                setStatus(null)
                if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
              }}>
              <option value="search">Search for object</option>
              <option value="survey">Survey coverage</option>
            </select>
          </label>
          <label>Target class
            <select aria-label="Target class" value={targetClass} disabled={mode === 'survey' || busy || !catalog || !enabled}
              onChange={(event) => {
                setTargetClass(event.target.value)
                setPreview(null)
                setStatus(null)
                if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
              }}>
              <option value="">Choose a target</option>
              {catalog?.target_classes.map((target) => <option key={target} value={target}>{target}</option>)}
            </select>
          </label>
          <button type="button" className="se-button" disabled={busy || !ready || !zoneId || (mode === 'search' && !targetClass) || !enabled || !services.search}
            onClick={() => { void prepare() }}>{busy ? 'Preparing coverage…' : mode === 'survey' ? 'Preview survey' : 'Preview search'}</button>
        </div>
        <p className="se-selection">Selected aircraft: {state.selection.length ? state.selection.map((id) => `D${id}`).join(', ') : 'none'}</p>
        {!ready && enabled && <p>Select ready aircraft with a connected console to preview a search.</p>}
        {error && <p role="alert">{error}</p>}
      </div>
      {preview?.intent_id === searchIntentId && <SearchPreviewView preview={preview} />}
      {status?.intent_id === searchIntentId && <SearchStatusView status={status} acknowledging={acknowledging} onAcknowledge={acknowledge} />}
    </section>
  )
}


function SearchPreviewView({ preview }: { preview: SearchPreview }) {
  return (
    <div className="se-panel">
      <h2>Frozen mission preview</h2>
      <p>{preview.preview.mode === 'survey' ? `${preview.preview.zone_id} · camera-evidenced survey` : `${preview.preview.zone_id} · ${preview.preview.target_class}`}</p>
      <SearchRoutes routes={preview.routes} />
      <table>
        <caption>Selected aircraft and coverage allocation</caption>
        <thead><tr><th>Aircraft</th><th>Camera source</th><th>Cells</th><th>Lanes</th></tr></thead>
        <tbody>{preview.preview.allocations.map((allocation) => <tr key={allocation.task_id}>
          <td>D{allocation.drone_id}</td><td>{allocation.source_id}</td><td>{allocation.workload_cells}</td><td>{allocation.lane_count}</td>
        </tr>)}</tbody>
      </table>
    </div>
  )
}

function SearchStatusView({ status, acknowledging, onAcknowledge }: {
  status: SearchStatus
  acknowledging: string | null
  onAcknowledge: (sightingId: string) => void
}) {
  const cells = status.tasks.flatMap((task) => task.cells.map((cell) => ({ ...cell, covered: task.covered_cell_ids.includes(cell.cell_id) })))
  return (
    <div className="se-status">
      <div className="se-panel">
        <h2>Coverage</h2>
        <p className="se-state">Mission <strong>{status.state}</strong> · {status.tasks.reduce((sum, task) => sum + task.covered_cells, 0)} / {status.tasks.reduce((sum, task) => sum + task.total_cells, 0)} cells covered</p>
        <CoveragePlot cells={cells} />
        <table>
          <caption>Search tasks</caption>
          <thead><tr><th>Aircraft</th><th>State</th><th>Coverage</th></tr></thead>
          <tbody>{status.tasks.map((task) => <tr key={task.task_id}><td>D{task.drone_id}</td><td>{task.state}</td><td>{task.covered_cells} / {task.total_cells}</td></tr>)}</tbody>
        </table>
      </div>
      <div className="se-panel">
        <h2>{status.mode === 'survey' ? 'Object findings' : 'Findings'}</h2>
        {status.candidates.length === 0 ? <p>{status.mode === 'survey' ? 'This survey does not identify objects.' : 'No candidate sightings have been reported.'}</p> : (
          <ul className="se-findings">{status.candidates.map((candidate) => <li key={candidate.sighting_id}>
            <strong>{candidate.label}</strong> · {(candidate.confidence * 100).toFixed(0)}% · {candidate.observation_count} observations
            {candidate.frame && <span> · {candidate.frame.source_id} / frame {candidate.frame.frame_id} · box {candidate.bbox_xyxy.join(', ')}</span>}
            {candidate.position && <span> · {candidate.position.zone_id}, {candidate.position.floor_id} at {candidate.position.x_m.toFixed(1)}, {candidate.position.y_m.toFixed(1)} m</span>}
            <button type="button" disabled={candidate.acknowledged || acknowledging === candidate.sighting_id}
              onClick={() => { void onAcknowledge(candidate.sighting_id) }}>
              {candidate.acknowledged ? 'Acknowledged' : acknowledging === candidate.sighting_id ? 'Acknowledging…' : 'Acknowledge finding'}
            </button>
          </li>)}</ul>
        )}
      </div>
    </div>
  )
}

function CoveragePlot({ cells }: { cells: Array<{ cell_id: string; x_m: number; y_m: number; covered: boolean }> }) {
  if (cells.length === 0) return <p>Coverage cells are not reported yet.</p>
  const minX = Math.min(...cells.map((cell) => cell.x_m))
  const maxX = Math.max(...cells.map((cell) => cell.x_m))
  const minY = Math.min(...cells.map((cell) => cell.y_m))
  const maxY = Math.max(...cells.map((cell) => cell.y_m))
  const x = (cell: typeof cells[number]) => ((cell.x_m - minX) / (maxX - minX || 1)) * 90 + 5
  const y = (cell: typeof cells[number]) => 95 - ((cell.y_m - minY) / (maxY - minY || 1)) * 90
  return <div className="se-plot" role="img" aria-label={`${cells.filter((cell) => cell.covered).length} of ${cells.length} coverage cells complete`}>
    {cells.map((cell) => <span key={cell.cell_id} className={cell.covered ? 'is-covered' : undefined}
      style={{ left: `${x(cell)}%`, top: `${y(cell)}%` }} />)}
  </div>
}


function SearchRoutes({ routes }: { routes: SearchPreview['routes'] }) {
  const points = routes.flatMap((route) => route.waypoints)
  const xs = points.map((point) => point[0]), ys = points.map((point) => point[1])
  const minX = Math.min(...xs), minY = Math.min(...ys)
  const width = Math.max(1, Math.max(...xs) - minX), height = Math.max(1, Math.max(...ys) - minY)
  const scale = Math.min(540 / width, 200 / height)
  const pixel = (point: [number, number, number]) => [30 + (point[0] - minX) * scale, 220 - (point[1] - minY) * scale]
  return <figure className="se-routes">
    <svg viewBox="0 0 600 250" role="img" aria-label="Frozen search flight routes in map coordinates">
      {routes.map((route, index) => <g key={route.drone_id} className={`se-route se-route-${index}`}>
        <polyline points={route.waypoints.map((point) => pixel(point).join(',')).join(' ')} />
        {route.waypoints.map((point, waypoint) => <circle key={waypoint} cx={pixel(point)[0]} cy={pixel(point)[1]} r={waypoint === 0 ? 5 : 3} />)}
        <text x={pixel(route.waypoints[0])[0] + 8} y={pixel(route.waypoints[0])[1] - 8}>D{route.drone_id} start</text>
      </g>)}
    </svg>
    <figcaption>Planned movement, viewed from above. Coordinates and height are in metres in the accepted map.</figcaption>
    <details><summary>Review waypoint coordinates</summary>
      <table><thead><tr><th>Aircraft</th><th>Waypoint</th><th>X</th><th>Y</th><th>Height</th></tr></thead>
        <tbody>{routes.flatMap((route) => route.waypoints.map((point, index) =>
          <tr key={`${route.drone_id}-${index}`}><td>D{route.drone_id}</td><td>{index === 0 ? 'Start' : index}</td>{point.map((value, axis) => <td key={axis}>{value.toFixed(2)}</td>)}</tr>,
        ))}</tbody></table>
    </details>
  </figure>
}
