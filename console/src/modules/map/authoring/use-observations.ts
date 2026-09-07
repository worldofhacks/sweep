import { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react'
import type { ControlState } from '../../../control/state'
import { formatDeviceId } from '../../../control/state'
import { DEVICE_FRESH_MS, observeDevice } from '../../../control/observation'
import { supports, type MapAuthoringClient } from './client'
import { canDraw } from './geometry'
import { currentWorldObservation, observationClock, POSITION_FRESH_MS, snapshotWorldObservation } from './observations'
import type { MapDraft, WorldPositionObservation } from './types'

interface Binding { client: MapAuthoringClient; context: string }
interface Batch {
  binding: Binding
  observations: WorldPositionObservation[]
  /** Kept when a stream error or expiry removes visible positions. */
  cursors: WorldPositionObservation[]
  error: string | null
}

export function useWorldObservations(client: MapAuthoringClient, draft: MapDraft, state: ControlState | undefined, now: () => number) {
  const session = client.status === 'available' ? client.sessionId : ''
  const enabled = supports(client, 'observe') && client.status === 'available' && Boolean(client.subscribePositions) && canDraw(draft)
    && state?.sessionId === session && ['connected', 'degraded'].includes(state.connection.status)
  const localNow = now()
  const at = observationClock(state, localNow)
  const { mapVersion, floorId } = draft.metadata
  const context = JSON.stringify({
    enabled, session, connection: state?.connection.status, stateSession: state?.sessionId,
    metadata: draft.metadata, image: draft.image && [draft.image.sha256, draft.image.width, draft.image.height],
    devices: Object.values(state?.aircraft ?? {}).map((device) => [device.drone_id, device.device_class,
      device.connection_epoch, observeDevice(device, at).state]).sort((left, right) => Number(left[0]) - Number(right[0])),
  })
  // Each transition gets a new identity, including A → B → A. Old subscription
  // callbacks and retained evidence can never regain ownership by value equality.
  const [binding, setBinding] = useState<Binding>(() => ({ client, context }))
  if (binding.client !== client || binding.context !== context) setBinding({ client, context })
  const [batch, setBatch] = useState<Batch | null>(null)
  const [, tick] = useReducer((count: number) => count + 1, 0)
  const latest = useRef({ state, metadata: draft.metadata, now, binding })
  useLayoutEffect(() => { latest.current = { state, metadata: draft.metadata, now, binding } }, [state, draft.metadata, now, binding])

  useEffect(() => {
    if (!enabled || client.status !== 'available' || !client.subscribePositions) return
    let active = true
    const empty: Batch = { binding, observations: [], cursors: [], error: null }
    const owns = () => active && latest.current.binding === binding
    const fail = (detail: string) => {
      if (!owns()) return
      setBatch((previous) => ({ ...(previous?.binding === binding ? previous : empty), observations: [], error: detail }))
    }
    const receive = (value: WorldPositionObservation) => {
      const live = latest.current
      const observation = snapshotWorldObservation(value)
      if (!owns() || !observation || !currentWorldObservation(observation, live.metadata, live.state, session, live.now())) return
      setBatch((previous) => {
        const own = previous?.binding === binding ? previous : empty
        const prior = own.cursors.find((item) => item.deviceId === observation.deviceId)
        if (prior && prior.connectionEpoch === observation.connectionEpoch && (observation.tCapture < prior.tCapture || observation.tIngest <= prior.tIngest || observation.observationId === prior.observationId)) return own
        return {
          binding,
          observations: [...own.observations.filter((item) => item.deviceId !== observation.deviceId), observation].slice(-64),
          cursors: [...own.cursors.filter((item) => item.deviceId !== observation.deviceId), observation].slice(-64),
          error: null,
        }
      })
    }
    let close: (() => void) | undefined
    try { close = client.subscribePositions({ mapVersion, floorId }, receive, fail) }
    catch { queueMicrotask(() => fail('The verified observation service could not connect.')) }
    return () => { active = false; close?.() }
  }, [binding, client, enabled, session, mapVersion, floorId])

  if (batch && batch.binding !== binding) setBatch(null)
  const own = enabled && batch?.binding === binding ? batch : null
  const current = (own?.observations ?? []).filter((observation) => currentWorldObservation(observation, draft.metadata, state, session, localNow))
  // Retire stale or invalid samples, instead of merely hiding them until a
  // context or clock value is restored. The reorder cursor survives retirement.
  if (own && current.length !== own.observations.length) setBatch({ ...own, observations: current })
  const positions = current.map((observation) => ({ observation, label: formatDeviceId(state!.aircraft[observation.deviceId]) }))
  const expiry = current.length ? Math.min(...current.map((observation) => Math.min(
    observation.tCapture + POSITION_FRESH_MS,
    state!.aircraft[observation.deviceId].last_seen_at! + DEVICE_FRESH_MS + 1,
  ))) : null
  const delay = expiry === null ? null : Math.max(1, Math.ceil(expiry - at + Math.max(0, (state?.lastStateEvent?.receivedAt ?? localNow) - localNow)))
  useEffect(() => {
    if (delay === null) return
    const timer = setTimeout(tick, delay)
    return () => clearTimeout(timer)
  }, [binding, delay, expiry])

  return {
    positions,
    reason: !enabled ? 'Verified live positions are unavailable. A supported observation service and current session/map/frame/epoch association are required.' : own?.error ?? (positions.length ? `${positions.length} fresh, verified world positions. Observations expire after one second.` : 'Waiting for fresh, verified world positions. No legacy telemetry is substituted.'),
  }
}
