import { useEffect, useRef, useState } from 'react'
import type { RequestRecord } from '../control/state'
import { formatDroneId } from '../control/state'
import type { InvalidationView } from './derive'
import { shortId } from './format'
import type { NavigationPlanPreview, NavigationPose } from '../relay/contract'

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
  const navigation = plan?.navigation
  const expiresAt = plan?.expiresAt
  const remainingMs = expiresAt === undefined ? null : Math.max(0, expiresAt - now)

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
          <span className="sh-dock-eyebrow">{navigation ? 'Route prepared — nothing sent' : pending.intent.name === 'navigate' ? 'Preparing route — nothing sent' : 'Pending — nothing sent'}</span>
          <br />
          <span className="sh-dock-title">{plan?.title ?? pending.intent.name}</span>{' '}
          <span className="sh-dock-targets">
            {pending.intent.selection.map(formatDroneId).join('  ') || 'whole roster'}
          </span>{' '}
          <span className="sh-dock-meta">
            roster v{plan?.rosterVersion ?? 'unreported'} · source {pending.intent.source} ·{' '}
            <span title={intentId}>{shortId(intentId)}</span>
          </span>
          {remainingMs !== null && (
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
        </p>
        <span className="sh-dock-actions">
          <button type="button" className="sh-confirm" disabled={pending.plan?.steps.length === 0 && !navigation} onClick={() => onConfirm(intentId)}>
            Confirm and send
          </button>
          <button type="button" className="sh-cancel" onClick={() => onCancel(intentId)}>
            Cancel
          </button>
        </span>
      </div>
      {navigation && <NavigationRoutePreview navigation={navigation} />}
      {pending.plan?.steps.length === 0 && !navigation && (
        <p className="sh-route-wait">The relay is preparing the route. Confirmation unlocks when its matching plan arrives.</p>
      )}
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

function NavigationRoutePreview({ navigation }: { navigation: NavigationPlanPreview }) {
  const poses = navigation.routes.flatMap((route) => [
    navigation.selected.find((selected) => selected.drone_id === route.drone)?.pose,
    ...route.waypoints,
    route.arrival_slot.pose,
  ].filter((pose): pose is NavigationPose => pose !== undefined))
  const bounds = routeBounds(poses)
  const point = (pose: NavigationPose) => `${mapCoordinate(pose.x_m, bounds.minX, bounds.maxX)} ${mapCoordinate(pose.y_m, bounds.minY, bounds.maxY, true)}`
  return (
    <section className="sh-route-preview" aria-label="Prepared navigation route">
      <div className="sh-route-heading">
        <p>Prepared route</p>
        <span>map {navigation.map_pin.version}</span>
      </div>
      <svg className="sh-route-map" viewBox="0 0 100 100" role="img" aria-label={`Route to ${navigation.destination_zone_id}`} preserveAspectRatio="xMidYMid meet">
        {navigation.routes.map((route) => {
          const routePoses = [navigation.selected.find((selected) => selected.drone_id === route.drone)?.pose, ...route.waypoints, route.arrival_slot.pose]
            .filter((pose): pose is NavigationPose => pose !== undefined)
          return <g key={route.drone}>
            <polyline points={routePoses.map(point).join(' ')} className="sh-route-line" />
            {routePoses[0] && <circle cx={mapCoordinate(routePoses[0].x_m, bounds.minX, bounds.maxX)} cy={mapCoordinate(routePoses[0].y_m, bounds.minY, bounds.maxY, true)} r="2.5" className="sh-route-start" />}
            <circle cx={mapCoordinate(route.arrival_slot.pose.x_m, bounds.minX, bounds.maxX)} cy={mapCoordinate(route.arrival_slot.pose.y_m, bounds.minY, bounds.maxY, true)} r="3" className="sh-route-arrival" />
          </g>
        })}
      </svg>
      <dl className="sh-route-details">
        <div><dt>Destination</dt><dd>{navigation.destination_zone_id}</dd></div>
        <div><dt>Aircraft</dt><dd>{navigation.execution_order.map(formatDroneId).join(' ')}</dd></div>
        <div><dt>Arrival slots</dt><dd>{navigation.routes.map((route) => `${formatDroneId(route.drone)} · ${route.arrival_slot.slot_id}`).join('; ')}</dd></div>
        <div><dt>After arrival</dt><dd>Hold at the assigned slot</dd></div>
      </dl>
    </section>
  )
}

function routeBounds(poses: NavigationPose[]) {
  const xs = poses.map((pose) => pose.x_m)
  const ys = poses.map((pose) => pose.y_m)
  return { minX: Math.min(...xs, 0), maxX: Math.max(...xs, 1), minY: Math.min(...ys, 0), maxY: Math.max(...ys, 1) }
}

function mapCoordinate(value: number, min: number, max: number, invert = false) {
  const span = Math.max(max - min, 1)
  const scaled = 10 + ((value - min) / span) * 80
  return invert ? 100 - scaled : scaled
}
