import { useEffect, useState } from 'react'
import type { NavigationCatalog } from '../../navigation/client'
import { RoutePreview } from '../../navigation/RoutePreview'
import type { ModuleProps } from '../types'

export function RoutesPane({ controller, services }: Pick<ModuleProps, 'controller' | 'services'>) {
  const { state, pendingRequest, prepareNavigation, cancelRequest } = controller
  const [catalog, setCatalog] = useState<NavigationCatalog | null>(null)
  const [zoneId, setZoneId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const enabled = state.enabledIntentNames.includes('navigate')
  useEffect(() => {
    if (!services.navigation || !enabled) return
    let cancelled = false
    services.navigation.catalog(state.sessionId).then(value => {
      if (!cancelled) { setCatalog(value); setError(null) }
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'Destinations could not be loaded.')
    })
    return () => { cancelled = true }
  }, [services.navigation, state.sessionId, state.connection, enabled])
  const selectedZone = catalog?.zones.find(zone => zone.zone_id === zoneId)
  const ready = state.connection.status === 'connected' && state.selection.length > 0 &&
    state.selection.every(id => state.aircraft[id]?.membership === 'ready' && state.aircraft[id]?.selectable)
  const prepare = async () => {
    setBusy(true)
    setError(null)
    try { await prepareNavigation(zoneId) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The route could not be prepared.') }
    finally { setBusy(false) }
  }
  return (
    <div className="ct-routes">
      <div className="ct-panel">
        <h2>Fly to a destination</h2>
        <p>Choose a mapped destination for the selected airborne aircraft. Review the route, then confirm in the dock.</p>
        {!enabled && <p role="status">Navigation is not configured on this relay.</p>}
        {enabled && !services.navigation && <p role="status">The navigation connection is unavailable.</p>}
        <label htmlFor="route-destination">Destination</label>
        <select id="route-destination" value={zoneId} disabled={busy || !catalog || !enabled}
          onChange={event => {
            setZoneId(event.target.value)
            if (pendingRequest?.intent.name === 'navigate') cancelRequest(pendingRequest.intent.intent_id)
          }}>
          <option value="">Choose a destination</option>
          {catalog?.zones.map(zone => <option key={zone.zone_id} value={zone.zone_id}
            disabled={!zone.navigation_allowed || zone.arrival_slots.length === 0}>
            {zone.aliases[0] ?? zone.zone_id}{!zone.navigation_allowed ? ' · unavailable' : ''}
          </option>)}
        </select>
        {selectedZone && <p>{selectedZone.zone_id} · {selectedZone.floor_id} · {selectedZone.arrival_slots.length} arrival slots</p>}
        <button type="button" className="ct-button" disabled={busy || !ready || !enabled || !services.navigation || !selectedZone?.navigation_allowed}
          onClick={() => { void prepare() }}>{busy ? 'Preparing route…' : 'Preview route'}</button>
        {!ready && enabled && <p>Select ready aircraft with a connected console to preview a route.</p>}
        {error && <p role="alert">{error}</p>}
      </div>
      {pendingRequest?.plan?.route && <div className="ct-panel"><RoutePreview preview={pendingRequest.plan.route} /></div>}
    </div>
  )
}
