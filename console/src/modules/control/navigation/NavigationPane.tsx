import { useState } from 'react'
import { navigationBlockedReason } from '../../../control/navigation'
import { formatDeviceId, type ControlState } from '../../../control/state'
import {
  resolveNavigationDestination,
  type NavigationPreview,
  type NavigationSnapshot,
  type NavigationTarget,
} from '../../../navigation'
import './navigation.css'

export interface NavigationPaneProps {
  state: ControlState
  snapshot: NavigationSnapshot
  now: number
  /** Requests a preview only. Confirmation and any future dispatch belong to the controller. */
  onPreview: (zoneId: string) => void
  onDestinationChange?: () => void
}

/** A destination identity is chosen here; coordinates and routes come from the planner. */
export function NavigationPane({ state, snapshot, now, onPreview, onDestinationChange }: NavigationPaneProps) {
  const [query, setQuery] = useState('')
  const [chosenId, setChosenId] = useState<string | null>(null)
  const catalog = snapshot.catalog
  const selected = state.selection.flatMap((id) => state.aircraft[id] ? [state.aircraft[id]] : [])
  const result = resolveNavigationDestination(catalog, chosenId ?? query, now, selected.map((device) => device.device_class))
  const search = query.trim().toLowerCase()
  const candidates = catalog?.destinations.filter((destination) =>
    !search || [destination.zoneId, destination.name, ...destination.aliases].some((name) => name.toLowerCase().includes(search)),
  ) ?? []
  const unavailable = snapshot.status === 'unavailable' || snapshot.status === 'error'
  const blocked = unavailable
    ? snapshot.reason ?? 'Named destinations are unavailable from the relay.'
    : snapshot.status === 'loading'
      ? 'Waiting for the destination review.'
      : navigationBlockedReason(state) ?? (result.kind === 'refused' ? result.reason : result.kind === 'ambiguous' ? 'Choose one canonical destination to resolve the ambiguity.' : null)

  function choose(zoneId: string, name: string) {
    setChosenId(zoneId)
    setQuery(name)
    onDestinationChange?.()
  }

  return (
    <section className="nv-pane" aria-label="Named-zone navigation">
      <div className="nv-heading">
        <h3 className="nv-title">Navigate to a destination</h3>
        <p className="nv-note">Choose a named destination from the accepted map, then review the planner’s route for each selected device.</p>
      </div>

      <div className="nv-section">
        <p className="nv-label">Selected devices · {state.selection.length}</p>
        {state.selection.length === 0 ? <p className="nv-note">Select devices in the fleet controls.</p> : (
          <ul className="nv-targets" aria-label="Navigation targets">
            {state.selection.map((id) => {
              const device = state.aircraft[id]
              return <li className="nv-target" key={id}>
                <strong>{device ? formatDeviceId(device) : `Device ${id}`}</strong>
                <span>{device ? `${device.device_class === 'aircraft' ? 'Aircraft' : 'Ground robot'} · epoch ${device.connection_epoch} · ${device.flight_state ?? 'state unreported'}` : 'Current device identity unavailable'}</span>
              </li>
            })}
          </ul>
        )}
      </div>

      {catalog && (
        <div className="nv-section">
          <p className="nv-note">Accepted map {catalog.map.mapId} · floor {catalog.map.floorId} · catalog {catalog.catalogVersion}</p>
          <label className="nv-search">
            <span className="nv-label">Destination name or alias</span>
            <input type="search" value={query} maxLength={256} autoComplete="off" placeholder="Search accepted destinations" onChange={(event) => {
              setQuery(event.target.value)
              setChosenId(null)
              onDestinationChange?.()
            }} />
          </label>
          {result.kind === 'ambiguous' && <p className="nv-note is-warning" role="status">This name matches more than one destination. Choose the intended destination below.</p>}
          {candidates.length === 0 ? <p className="nv-note">No matching destination in the accepted map.</p> : (
            <div className="nv-zones" role="group" aria-label="Matching destinations">
              {candidates.map((destination) => (
                <button className="nv-zone" type="button" key={destination.zoneId} aria-pressed={result.kind === 'resolved' && result.destination.zoneId === destination.zoneId} onClick={() => choose(destination.zoneId, destination.name)}>
                  <span className="nv-zone-name">{destination.name}</span>
                  <span className="nv-zone-meta">{destination.zoneId} · floor {destination.floorId}{destination.excluded ? ' · excluded' : destination.reachability === 'unreachable' ? ' · unreachable' : destination.reachability === 'unknown' ? ' · reachability unreported' : ''}</span>
                  {destination.aliases.length > 0 && <span className="nv-zone-meta">Aliases: {destination.aliases.join(', ')}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {blocked && <p className="nv-note is-warning" role="status">{blocked}</p>}
      <button type="button" className="nv-review" disabled={blocked !== null || result.kind !== 'resolved'} onClick={() => {
        if (blocked === null && result.kind === 'resolved') onPreview(result.destination.zoneId)
      }}>Review destination</button>
      <p className="nv-note">Navigation execution is unavailable until the relay provides a frozen confirmation contract. Reviewing a destination does not send a motion command.</p>

      {snapshot.preview && <NavigationPreviewDetails preview={snapshot.preview} now={now} />}
    </section>
  )
}

function targetName(target: NavigationTarget): string {
  return `${target.deviceClass === 'aircraft' ? 'Aircraft' : 'Ground robot'} ${target.id} · epoch ${target.epoch}`
}

/** Shared with the central dock; every route, slot, and outcome is the reported frozen value. */
export function NavigationPreviewDetails({ preview, now }: { preview: NavigationPreview; now?: number }) {
  return (
    <section className="nv-preview nv-section" aria-label="Navigation planner preview">
      <div className="nv-heading">
        <h3 className="nv-title">Review: {preview.destination.name}</h3>
        <p className="nv-note">{preview.destination.zoneId} · floor {preview.destination.floorId}</p>
      </div>
      {now !== undefined && now >= preview.expiresAt && <p className="nv-note is-warning" role="status">Preview expired. Request a new destination review.</p>}
      <dl className="nv-versions">
        <Version label="Map" value={preview.map.mapId} />
        <Version label="Map pin" value={preview.map.mapPin} />
        <Version label="Geometry pin" value={preview.map.geometryPin} />
        <Version label="Navigation pin" value={preview.map.navigationPin} />
        <Version label="Catalog version" value={preview.catalogVersion} />
        <Version label="Configuration version" value={preview.configVersion} />
        <Version label="Roster version" value={preview.rosterVersion} />
      </dl>
      <ul className="nv-routes" aria-label="Per-device navigation outcomes">
        {preview.selected.map((target) => {
          const matches = (candidate: NavigationTarget) => candidate.id === target.id && candidate.epoch === target.epoch && candidate.deviceClass === target.deviceClass
          const outcome = preview.outcomes.find((item) => matches(item.target))
          const route = preview.routes.find((item) => matches(item.target))
          return <li className="nv-route" key={`${target.id}:${target.epoch}`}>
            <h4>{targetName(target)}</h4>
            <p className={outcome?.status === 'refused' ? 'nv-note is-danger' : 'nv-note'}>{outcome ? `${outcome.status === 'refused' ? 'Refused' : 'Planned'} · ${outcome.code}: ${outcome.detail}` : 'Planner outcome unreported.'}</p>
            {route && outcome?.status === 'planned' && <>
              <ol className="nv-waypoints" aria-label={`Route for ${targetName(target)}`}>
                {route.waypoints.map((point, index) => <li key={index}>x {point.xM}, y {point.yM}, z {point.zM} m · {point.floorId}</li>)}
              </ol>
              <p>Arrival slot {route.arrivalSlot.slotId} in {route.arrivalSlot.zoneId}: x {route.arrivalSlot.position.xM}, y {route.arrivalSlot.position.yM}, z {route.arrivalSlot.position.zM} m.</p>
              <p>On arrival: {route.holdBehavior === 'hover' ? 'hover and hold position' : 'stop and hold position'}.</p>
            </>}
          </li>
        })}
      </ul>
      <details className="nv-config"><summary>Frozen motion configuration</summary><pre>{JSON.stringify(preview.motionConfig, null, 2)}</pre></details>
      <p className="nv-note">This review grants no takeoff, capture, survey, or formation action. Navigation confirmation and execution remain unavailable.</p>
    </section>
  )
}

function Version({ label, value }: { label: string; value: unknown }) {
  return <div><dt>{label}</dt><dd>{typeof value === 'string' || typeof value === 'number' ? value : JSON.stringify(value)}</dd></div>
}
