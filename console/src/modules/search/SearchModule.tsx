import { useEffect, useMemo, useState } from 'react'
import { RoutePreview } from '../../navigation/RoutePreview'
import type { SearchCatalog, SearchStatus } from '../../search/client'
import './search.css'
import type { ModuleProps } from '../types'

const POLL_MS = 2_000

export function SearchModule({ controller, services }: ModuleProps) {
  const { state, pendingRequest, prepareSearch, cancelRequest } = controller
  const [catalog, setCatalog] = useState<SearchCatalog | null>(null)
  const [zoneId, setZoneId] = useState('')
  const [targetClass, setTargetClass] = useState('')
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
    try { await prepareSearch(zoneId, targetClass) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The search could not be prepared.') }
    finally { setBusy(false) }
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
  const route = pendingRequest?.intent.name === 'search' ? pendingRequest.plan?.route : undefined

  return (
    <section className="se-module" aria-label="Visual search">
      <div className="se-panel">
        <h2>Search configuration</h2>
        <p>Select the configured room and target class. The relay freezes both the route and coverage tasks before confirmation.</p>
        {!enabled && <p role="status">Search is not configured on this relay.</p>}
        {enabled && !services.search && <p role="status">The search connection is unavailable.</p>}
        <div className="se-controls">
          <label>Room
            <select aria-label="Search room" value={zoneId} disabled={busy || !catalog || !enabled}
              onChange={(event) => {
                setZoneId(event.target.value)
                if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
              }}>
              <option value="">Choose a room</option>
              {catalog?.zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
            </select>
          </label>
          <label>Target class
            <select aria-label="Target class" value={targetClass} disabled={busy || !catalog || !enabled}
              onChange={(event) => {
                setTargetClass(event.target.value)
                if (pendingRequest?.intent.name === 'search') cancelRequest(pendingRequest.intent.intent_id)
              }}>
              <option value="">Choose a target</option>
              {catalog?.target_classes.map((target) => <option key={target} value={target}>{target}</option>)}
            </select>
          </label>
          <button type="button" className="se-button" disabled={busy || !ready || !zoneId || !targetClass || !enabled || !services.search}
            onClick={() => { void prepare() }}>{busy ? 'Preparing search…' : 'Preview search'}</button>
        </div>
        {!ready && enabled && <p>Select ready aircraft with a connected console to preview a search.</p>}
        {error && <p role="alert">{error}</p>}
      </div>
      {route && <div className="se-panel"><RoutePreview preview={route} /></div>}
      {status?.intent_id === searchIntentId && <SearchStatusView status={status} acknowledging={acknowledging} onAcknowledge={acknowledge} />}
    </section>
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
        <h2>Findings</h2>
        {status.candidates.length === 0 ? <p>No candidate sightings have been reported.</p> : (
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
