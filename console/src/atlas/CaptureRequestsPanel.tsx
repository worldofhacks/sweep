import { useState } from 'react'
import { Icon } from './Icon'
import { captureRequestContext, isActionableRequest, isCurrentSurfaceRequest, spaceCaptureRequests } from './captureRequests'
import type { CaptureRequestContext, SpaceDetail, SpaceRequest, SurfaceRequest } from './types'

export function CaptureRequestsPanel({ detail, onInspect, onContribute, onViewCaptures, canContribute = true }: {
  canContribute?: boolean
  detail: SpaceDetail
  onInspect: (request: SpaceRequest | SurfaceRequest) => void
  onContribute: (request: CaptureRequestContext) => void
  onViewCaptures: (request: CaptureRequestContext) => void
}) {
  const [history, setHistory] = useState(false)
  const requests = spaceCaptureRequests(detail)
  const open = requests.filter(item => isActionableRequest(item, detail))
  const past = requests.length - open.length
  return <section className="atlas-requests" aria-label="Capture requests">
    <div className="atlas-section-heading">
      <span className="atlas-eyebrow">ADD THE NEXT PERSPECTIVE</span>
      <h3>{open.length ? `${open.length} ${open.length === 1 ? 'view requested' : 'views requested'}.` : 'No open requests.'}</h3>
      <p>{detail.space.status === 'resolved' ? 'This space is resolved. Previous requests and their originals remain available.'
        : open.length ? 'See what would help, inspect the area, then add your view.' : 'New requests will appear here when someone asks for another view.'}</p>
    </div>
    {(history ? requests : open).map(item => {
      const location = 'cell_id' in item
      const actionable = isActionableRequest(item, detail)
      const current = location || isCurrentSurfaceRequest(item, detail.reconstruction)
      const context = captureRequestContext(item)
      const label = location ? `Map area ${item.cell_id}` : item.label
      return <article className="atlas-surface-request" aria-label={label} key={JSON.stringify(context.target)}>
        <div className="atlas-request-heading"><Icon name={location ? 'pin' : 'cube'} size={20} />
          <div><span className="atlas-eyebrow">{location ? 'REQUESTED LOCATION' : '3D MODEL REGION'}</span><strong>{label}</strong></div>
          <span className="atlas-request-state">{actionable ? 'Open' : detail.space.status !== 'active' ? 'Space resolved'
            : item.status === 'captured' ? 'Location captured' : item.status === 'dismissed' ? 'Dismissed' : 'Earlier build'}</span>
        </div>
        <p>{item.note}</p>
        {!current && <p className="atlas-fine">This target belongs to an earlier model. It cannot be highlighted on the current build.</p>}
        <div className="atlas-request-actions">
          {current && <button className="atlas-secondary" onClick={() => onInspect(item)}>{location ? 'Show requested area' : 'Inspect in 3D'}</button>}
          {actionable && canContribute && <button className="atlas-primary" onClick={() => onContribute(context)}>Contribute this view</button>}
          {!!item.capture_ids?.length && <button className="atlas-text-button" onClick={() => onViewCaptures(context)}>
            View {item.capture_ids.length} linked {item.capture_ids.length === 1 ? 'capture' : 'captures'}
          </button>}
        </div>
      </article>
    })}
    {past > 0 && <button className="atlas-text-button" aria-pressed={history} onClick={() => setHistory(value => !value)}>
      {history ? 'Hide previous requests' : `Show ${past} previous ${past === 1 ? 'request' : 'requests'}`}
    </button>}
    <p className="atlas-fine">Only contribute where safe and permitted. Requested map locations are not safe routes; model regions have no GPS position. Uploads do not certify surface completeness.</p>
  </section>
}
