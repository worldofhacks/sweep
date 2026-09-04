import type { ControlState, RequestRecord } from '../../control/state'
import { formatDroneId, isTerminalRequest } from '../../control/state'
import { formatSelection, formatTime, humanizeCode, shortId } from '../../shell/format'
import { EmptyState, PanelHeading } from '../shared'
import type { ModuleProps } from '../types'

/** The visible request lifecycle and last outcome, moved from the checkpoint dashboard unchanged. */
export function RequestsPanel({ controller }: { controller: ModuleProps['controller'] }) {
  const { state, retryFailedRequest } = controller
  return (
    <section className="panel request-panel" aria-labelledby="request-title">
      <PanelHeading
        eyebrow="Visible lifecycle"
        title="Requests"
        meta={`${state.requests.length} this view`}
        id="request-title"
      />
      {state.lastOutcome && (
        <div className={`last-outcome outcome-${state.lastOutcome.kind}`}>
          <span className="eyebrow">Last acknowledgement / refusal</span>
          <strong>{humanizeCode(state.lastOutcome.status)}</strong>
          <p>{state.lastOutcome.detail}</p>
          {state.lastOutcome.reasonCode && <code>{state.lastOutcome.reasonCode}</code>}
        </div>
      )}
      {state.requests.length === 0 ? (
        <EmptyState title="No requests" detail="Button and keyboard intents will appear here." />
      ) : (
        <ol className="request-list">
          {state.requests.slice(0, 8).map((request) => (
            <RequestItem
              key={request.intent.intent_id}
              request={request}
              state={state}
              onRetry={retryFailedRequest}
            />
          ))}
        </ol>
      )}
    </section>
  )
}

function RequestItem({ request, state, onRetry }: {
  request: RequestRecord
  state: ControlState
  onRetry: (request: RequestRecord) => void
}) {
  const retryReason = getRetryBlockedReason(request, state)
  return (
    <li className="request-item">
      <div className="request-topline">
        <div>
          <strong>{humanizeCode(request.intent.name)}</strong>
          <span className={`status-label status-${request.status}`}>{humanizeCode(request.status)}</span>
        </div>
        <code title={request.intent.intent_id}>{shortId(request.intent.intent_id)}</code>
      </div>
      <p className="request-target">
        {request.intent.selection.length > 0 ? formatSelection(request.intent.selection) : 'All aircraft'} ·{' '}
        {request.intent.source}
      </p>
      {request.intent.retry_of && <p className="retry-link">Retry of <code>{shortId(request.intent.retry_of)}</code></p>}
      {(request.reasonCode || request.detail) && (
        <div className="request-reason">
          {request.reasonCode && <code>{request.reasonCode}</code>}
          {request.detail && <span>{request.detail}</span>}
        </div>
      )}
      <div className="timestamp-row" aria-label="Request lifecycle timestamps">
        {Object.entries(request.timestamps).map(([status, t]) => (
          <span key={status}>{humanizeCode(status)} <time className="mono">{formatTime(t)}</time></span>
        ))}
      </div>
      {request.status === 'failed' && (
        <button
          type="button"
          className="text-button retry-button"
          disabled={Boolean(retryReason)}
          title={retryReason ?? undefined}
          onClick={() => onRetry(request)}
        >
          Retry as new intent
        </button>
      )}
      {!isTerminalRequest(request.status) && request.status !== 'pending_confirmation' && (
        <span className="in-flight-label">Awaiting terminal result</span>
      )}
    </li>
  )
}

function getRetryBlockedReason(request: RequestRecord, state: ControlState): string | null {
  if (request.status !== 'failed') return 'Only terminal failed requests can be retried.'
  const connection = request.intent.source === 'keyboard' ? state.keyboardConnection : state.connection
  if (connection.status !== 'connected') return `${request.intent.source} relay source is unavailable.`
  const unavailable = request.intent.selection.find(
    (id) => state.aircraft[id]?.membership !== 'ready' || !state.aircraft[id]?.selectable,
  )
  if (unavailable) return `${formatDroneId(unavailable)} is no longer ready; no substitute will be selected.`
  return null
}
