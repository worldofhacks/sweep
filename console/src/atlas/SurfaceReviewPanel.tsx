import { useEffect, useRef, useState } from 'react'
import type { AtlasClient } from './client'
import { Icon } from './Icon'
import type { CaptureRequestContext, Reconstruction, SurfaceFocus, SurfaceRequest } from './types'
import { captureRequestContext, isCurrentSurfaceRequest } from './captureRequests'

const INITIAL_NOTE = 'If safe and permitted, add overlapping photos from different positions around the highlighted edge. Include recognizable surrounding detail.'

export function SurfaceReviewPanel({ job, requests, client, spaceId, canManage, active, onFocus, onChange, onContribute, onViewCaptures, initialRegionId }: {
  job: Reconstruction
  requests: SurfaceRequest[]
  client: AtlasClient
  spaceId: string
  canManage: boolean
  active: boolean
  onFocus: (focus: SurfaceFocus | null) => void
  onChange: () => void
  onContribute?: (request: CaptureRequestContext) => void
  onViewCaptures?: (request: CaptureRequestContext) => void
  initialRegionId?: string
}) {
  const [selectedId, setSelectedId] = useState<string | null>(initialRegionId ?? null)
  const [inspection, setInspection] = useState(0)
  const [note, setNote] = useState(INITIAL_NOTE)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(initialRegionId ? 'Loading review edges…' : '')
  const [error, setError] = useState('')
  const [inspected, setInspected] = useState<SurfaceFocus | null>(null)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const review = job.status === 'ready' ? job.surface_review : null
  const selected = review?.regions.find(region => region.id === selectedId)
  const selectedRequest = requests.find(item => item.job_id === job.id && item.region_id === selectedId)
  const open = requests.filter(item => item.status === 'open')
  const run = async (action: () => Promise<unknown>, success: string) => {
    if (busy) return
    setBusy(true); setError(''); setMessage('')
    try { await action(); onChange(); setMessage(success) }
    catch (value) { setError(value instanceof Error ? value.message : 'The request could not be saved. Try again.') }
    finally { setBusy(false) }
  }
  const available = Boolean(selected)
  useEffect(() => {
    if (!available || !selectedId || !job.id || !job.artifact_sha256) return
    const controller = new AbortController()
    pending.current = controller
    void client.surfaceRegion(spaceId, job.id, job.artifact_sha256, selectedId, controller.signal).then(region => {
      if (controller.signal.aborted) return
      const focus = { job_id: job.id!, artifact_sha256: job.artifact_sha256!, region }
      setInspected(focus); onFocus(focus); setMessage('')
    }).catch(value => {
      if (!controller.signal.aborted) { setMessage(''); setError(value instanceof Error ? value.message : 'Review edges could not be loaded. Select the region to retry.') }
    })
    return () => controller.abort()
  }, [available, selectedId, inspection, client, spaceId, job.id, job.artifact_sha256, onFocus])
  const inspect = (id: string) => {
    if (!review?.regions.some(item => item.id === id)) return
    pending.current?.abort()
    setSelectedId(id); setInspection(value => value + 1); setInspected(null); setMessage('Loading review edges…'); setError('')
    onFocus(null)
  }
  return (
    <section className="atlas-surface-review" aria-labelledby="surface-review-heading">
      <div className="atlas-section-heading">
        <span className="atlas-eyebrow">THE NEXT PERSPECTIVE</span>
        <h3 id="surface-review-heading">Surface review</h3>
        <p>Inspect open model edges before asking for more views.</p>
      </div>
      {!review ? <p className="atlas-fine">Build a photo-textured mesh to identify review regions. Sparse points and GPS cells cannot establish surface gaps.</p>
        : review.regions.length === 0 ? <p className="atlas-fine">No review regions met this detector’s limits. This does not prove the scene is complete; inspect the model and originals.</p>
          : <>
            <p className="atlas-fine">Showing {review.regions.length} of {review.candidate_regions ?? review.regions.length} edge groups, ranked by boundary length—not urgency. These may be natural edges or the end of a scan, not missing surfaces.</p>
            <div className="atlas-surface-regions" aria-label="Model review regions">
              {review.regions.map(region => <button key={region.id} className="atlas-secondary"
                aria-pressed={selectedId === region.id} onClick={() => inspect(region.id)}>
                <Icon name="target" size={17} /> {region.label}
              </button>)}
            </div>
            {selected && <div className="atlas-surface-selection">
              <strong>{selected.label} · {selected.boundary_edges.toLocaleString()} open edges</strong>
              <p className="atlas-fine">Light lines mark a sample of actual model edges, including hidden edges. Orbit to inspect. No safe route, GPS target, or physical distance is established.</p>
              <button className="atlas-text-button" onClick={() => { pending.current?.abort(); setSelectedId(null); setInspected(null); setMessage(''); onFocus(null) }}>Show whole model</button>
              {canManage && !selectedRequest && <>
                <label className="atlas-label" htmlFor="surface-request-note">What would help?</label>
                <textarea id="surface-request-note" maxLength={240} rows={4} value={note}
                  onChange={event => setNote(event.target.value)} />
                <button className="atlas-primary atlas-full" disabled={busy || !active || !inspected || note.trim().length < 3}
                  onClick={() => void run(() => client.requestSurface(spaceId, inspected!, note.trim()), 'Request shared with contributors in this space.')}>
                  <Icon name="plus" size={17} /> {busy ? 'Saving…' : 'Request more views'}
                </button>
              </>}
              {selectedRequest && <p className="atlas-fine">{selectedRequest.status === 'open'
                ? 'A request is already open for this region.' : 'This request was dismissed. Review again after the next build.'}</p>}
              {!active && <p className="atlas-fine">This space is resolved. Reopen it to request more views.</p>}
            </div>}
          </>}
      <h4>Shared capture requests{open.length ? ` · ${open.length}` : ''}</h4>
      {open.length === 0 && <p className="atlas-fine">No open surface requests. Requests appear here for everyone with space access; push notifications are not enabled.</p>}
      {requests.filter(item => item.status === 'open' || item.capture_ids?.length).map(item => {
        const current = isCurrentSurfaceRequest(item, job)
        return <article className="atlas-surface-request" key={`${item.job_id}:${item.region_id}`}>
          <strong>{item.label} · {item.status === 'open' ? 'More views requested' : 'Request dismissed'}</strong>
          <p>{item.note}</p>
          {current ? <button className="atlas-secondary atlas-full" onClick={() => inspect(item.region_id)}>
            <Icon name="target" size={17} /> Inspect requested region
          </button> : <p className="atlas-fine">From an earlier build. Its highlight cannot be applied to the current model. Review the rebuilt scene before making a new request.</p>}
          <p className="atlas-fine">New captures do not automatically close this request or prove the edge is filled.</p>
          <div className="atlas-request-actions">
            {onContribute && current && item.status === 'open' && <button className="atlas-primary"
              disabled={!active} onClick={() => onContribute(captureRequestContext(item))}>Contribute this view</button>}
            {onViewCaptures && !!item.capture_ids?.length && <button className="atlas-secondary"
              onClick={() => onViewCaptures(captureRequestContext(item))}>View {item.capture_ids.length} linked {item.capture_ids.length === 1 ? 'capture' : 'captures'}</button>}
          </div>
          {canManage && item.status === 'open' && <button className="atlas-text-button" disabled={busy}
            onClick={() => void run(() => client.dismissSurfaceRequest(spaceId, item.job_id, item.region_id), 'Request dismissed; no surface completeness was asserted.')}>
            Dismiss request
          </button>}
        </article>
      })}
      {message && <p role="status" className="atlas-fine">{message}</p>}
      {error && <p role="alert" className="atlas-inline-notice">{error}</p>}
    </section>
  )
}
