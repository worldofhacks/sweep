import type { ControlState, RequestRecord, RequestStatus } from '../../control/state'
import { formatDroneId, isTerminalRequest } from '../../control/state'
import { requiresConfirmation } from '../../relay/contract'
import { formatTime, shortId } from '../../shell/format'
import { reasonSentence } from '../../shell/sentences'
import type { ModuleProps } from '../types'
import { requestTone, retryBlockedReason } from './controls'

const SHOWN_REQUESTS = 14

/** Canonical lifecycle order for the timeline chips. */
const TIMELINE_ORDER: RequestStatus[] = [
  'draft',
  'pending_confirmation',
  'sent',
  'accepted',
  'refused',
  'executing',
  'completed',
  'failed',
  'invalidated',
  'cancelled',
]

/** Control › Requests: the newest outcome as a card, then the recent requests. */
export function RequestsPane({ controller }: { controller: ModuleProps['controller'] }) {
  const { state, retryRequest } = controller
  const outcome = state.requests.find((request) => isTerminalRequest(request.status)) ?? null
  const shown = state.requests.slice(0, SHOWN_REQUESTS)
  return (
    <div>
      {outcome && <OutcomeCard request={outcome} />}
      {shown.length === 0 ? (
        <p className="ct-requests-empty">No requests in this session. Every control press appears here.</p>
      ) : (
        <ol className="ct-requests" aria-label="Requests">
          {shown.map((request) => (
            <RequestRow key={request.intent.intent_id} request={request} state={state} onRetry={retryRequest} />
          ))}
        </ol>
      )}
    </div>
  )
}

function OutcomeCard({ request }: { request: RequestRecord }) {
  const tone = requestTone(request.status)
  const sentence = reasonSentence(request.reasonCode) || `The relay reported the request ${request.status}.`
  return (
    <div className={`ct-outcome tone-${tone}`} aria-live="polite" aria-label="Latest outcome">
      <p className="ct-outcome-head">
        <span className="ct-outcome-name">{request.intent.name}</span>{' '}
        <span className={`ct-outcome-state tone-${tone}`}>{request.status}</span>
      </p>
      <p className="ct-outcome-sentence">{sentence}</p>
      {request.detail && <p className="ct-outcome-detail">{request.detail}</p>}
    </div>
  )
}

function RequestRow({
  request,
  state,
  onRetry,
}: {
  request: RequestRecord
  state: ControlState
  onRetry: (request: RequestRecord) => void
}) {
  const tone = requestTone(request.status)
  const intent = request.intent
  const canRetry = request.status === 'failed' || request.status === 'refused'
  const retryBlocked = canRetry ? retryBlockedReason(request, state) : null
  const timeline = TIMELINE_ORDER.filter((status) => request.timestamps[status] !== undefined)
  return (
    <li className="ct-request" aria-label={`${intent.name} ${request.status}`}>
      <div className="ct-request-head">
        <span className="ct-request-name">{intent.name}</span>{' '}
        <span className={`ct-request-state tone-${tone}`}>{request.status}</span>{' '}
        <span className="ct-request-id" title={intent.intent_id}>
          {shortId(intent.intent_id)}
        </span>{' '}
        <span className="ct-request-targets">{intent.selection.map(formatDroneId).join(' ') || 'fleet'}</span>{' '}
        <span className="ct-request-source">source {intent.source}</span>
      </div>
      {intent.retry_of && (
        <p className="ct-request-retry-of">
          Retry of <code title={intent.retry_of}>{shortId(intent.retry_of)}</code>
        </p>
      )}
      {request.reasonCode && (
        <p className="ct-request-reason">
          <code className={`tone-${tone}`}>{request.reasonCode}</code>
          {reasonSentence(request.reasonCode) ? ` — ${reasonSentence(request.reasonCode)}` : ''}
        </p>
      )}
      {request.detail && <p className="ct-request-detail">{request.detail}</p>}
      <p className="ct-timeline" aria-label="Lifecycle timestamps">
        {timeline.map((status) => (
          <span key={status}>
            {status} <time>{formatTime(request.timestamps[status] as number)}</time>
          </span>
        ))}
      </p>
      {canRetry && (
        <div className="ct-retry-wrap">
          <button
            type="button"
            className="ct-retry"
            disabled={retryBlocked !== null}
            onClick={() => onRetry(request)}
          >
            Retry as new intent
          </button>
          <p className="ct-retry-note">
            {retryBlocked ??
              (requiresConfirmation(intent.name)
                ? 'Mints a new intent id, sets retry_of to this request, and opens a new preview to confirm.'
                : 'Mints a new intent id and sets retry_of to this request.')}
          </p>
        </div>
      )}
    </li>
  )
}
