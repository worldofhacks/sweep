import { useEffect, useRef, useState } from 'react'
import type { RequestRecord } from '../control/state'
import { formatDroneId } from '../control/state'
import type { InvalidationView } from './derive'
import { shortId } from './format'

const COUNTDOWN_URGENT_MS = 15_000

export interface DockProps {
  pending: RequestRecord | null
  invalidation: InvalidationView | null
  /** Wall-clock at render; the shell ticks it while a countdown is showing. */
  now: number
  onConfirm: (intentId: string) => void
  onCancel: (intentId: string) => void
}

/**
 * Footer dock. Shows the one pending plan until it is confirmed, cancelled, or
 * invalidated; otherwise the newest never-sent invalidation, if any.
 */
export function Dock({ pending, invalidation, now, onConfirm, onCancel }: DockProps) {
  if (pending) {
    return (
      <PendingPlan
        key={pending.intent.intent_id}
        pending={pending}
        now={now}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }
  if (invalidation) {
    return (
      <p className="sh-invalidated" role="alert">
        <strong>Preview invalidated, nothing sent</strong> — <code>{invalidation.reasonCode}</code>{' '}
        {invalidation.detail}
      </p>
    )
  }
  return null
}

function PendingPlan({
  pending,
  now,
  onConfirm,
  onCancel,
}: {
  pending: RequestRecord
  now: number
  onConfirm: (intentId: string) => void
  onCancel: (intentId: string) => void
}) {
  const region = useRef<HTMLDivElement>(null)
  const [jsonOpen, setJsonOpen] = useState(true)
  const intentId = pending.intent.intent_id
  const plan = pending.plan
  const expiresAt = plan?.expiresAt
  const remainingMs = expiresAt === undefined ? null : Math.max(0, expiresAt - now)
  const expired = remainingMs === 0

  useEffect(() => {
    region.current?.focus()
  }, [intentId])

  return (
    <div
      className="sh-dock"
      role="region"
      aria-label="Pending confirmation"
      aria-live="polite"
      tabIndex={-1}
      ref={region}
    >
      <div className="sh-dock-row">
        <p className="sh-dock-summary">
          <span className="sh-dock-eyebrow">Pending — nothing sent</span>
          <br />
          <span className="sh-dock-title">{plan?.title ?? pending.intent.name}</span>{' '}
          <span className="sh-dock-targets">
            {pending.intent.selection.map(formatDroneId).join('  ') || 'whole roster'}
          </span>{' '}
          <span className="sh-dock-meta">
            roster v{plan?.rosterVersion ?? 'unreported'} · source {pending.intent.source} ·{' '}
            <span title={intentId}>{shortId(intentId)}</span>
          </span>
          {remainingMs !== null && !expired && (
            <span
              className={
                remainingMs < COUNTDOWN_URGENT_MS ? 'sh-dock-countdown is-urgent' : 'sh-dock-countdown'
              }
              aria-hidden="true"
            >
              {' '}
              confirm within {Math.round(remainingMs / 1000)} s
            </span>
          )}
          {expired && (
            <span className="sh-dock-countdown is-expired" role="status">
              {' '}
              confirmation window expired — cancel and say it again
            </span>
          )}
        </p>
        <span className="sh-dock-actions">
          <button
            type="button"
            className="sh-confirm"
            disabled={expired}
            title={expired ? 'The confirmation window expired; nothing can be sent from this preview.' : undefined}
            onClick={() => onConfirm(intentId)}
          >
            Confirm and send
          </button>
          <button type="button" className="sh-cancel" onClick={() => onCancel(intentId)}>
            Cancel
          </button>
        </span>
      </div>
      {plan && plan.steps.length > 0 && (
        <ol className="sh-dock-steps">
          {plan.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      )}
      <button
        type="button"
        className="sh-json-toggle"
        aria-expanded={jsonOpen}
        onClick={() => setJsonOpen((open) => !open)}
      >
        {jsonOpen ? 'Hide Intent v1 envelope' : 'Show Intent v1 envelope'}
      </button>
      {jsonOpen && (
        <div className="sh-json">
          <p className="sh-json-note">
            Exact Intent v1 draft. Confirming stamps t and sets confirm true; nothing else changes.
          </p>
          <pre className="sh-json-pre" data-scroll="1">
            {JSON.stringify(pending.intent, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
