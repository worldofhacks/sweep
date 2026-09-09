import type { CaptureRequestContext } from './types'

export function CaptureRequestNotice({ request }: { request: CaptureRequestContext }) {
  return <aside className="atlas-request-context" aria-label="Requested view">
    <span className="atlas-eyebrow">CONTRIBUTING TO A REQUEST</span>
    <strong>{request.label}</strong>
    <p>{request.note}</p>
    <p className="atlas-fine">Only contribute where safe and permitted. Your upload will be linked to this request; it does not certify coverage or a safe route.</p>
  </aside>
}
