import { useSyncExternalStore } from 'react'
import type { DroneId, RelaySensorEvent } from '../relay/contract'

/** Scans kept per device for trails, oldest first. */
export const SENSOR_TRAIL_LENGTH = 20

export interface SensorSnapshot {
  /** The newest accepted scan per device. */
  latest: Readonly<Record<DroneId, RelaySensorEvent>>
  /** Up to the last SENSOR_TRAIL_LENGTH accepted scans per device, oldest first. */
  trails: Readonly<Record<DroneId, readonly RelaySensorEvent[]>>
}

export interface SensorStore {
  snapshot(): SensorSnapshot
  subscribe(listener: () => void): () => void
  /**
   * Records a scan and returns whether it was kept. A frame older than the
   * device's newest scan, or a repeat of it, is ignored; a new connection
   * epoch starts a fresh trail.
   */
  apply(event: RelaySensorEvent): boolean
  reset(): void
}

const EMPTY: SensorSnapshot = { latest: {}, trails: {} }

/**
 * Scans live outside the control reducer: they arrive at up to 5 Hz per
 * device and carry hundreds of ranges each, and nothing in the control flow
 * depends on them. The reducer records only that a scan arrived.
 */
export function createSensorStore(): SensorStore {
  let snapshot: SensorSnapshot = EMPTY
  const listeners = new Set<() => void>()
  const notify = () => listeners.forEach((listener) => listener())
  return {
    snapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    apply(event) {
      const previous = snapshot.latest[event.drone_id]
      if (previous !== undefined) {
        if (previous.event_id === event.event_id) return false
        if (previous.connection_epoch === event.connection_epoch && event.t < previous.t) return false
      }
      const sameEpoch = previous !== undefined && previous.connection_epoch === event.connection_epoch
      const trail = [...(sameEpoch ? (snapshot.trails[event.drone_id] ?? []) : []), event].slice(
        -SENSOR_TRAIL_LENGTH,
      )
      snapshot = {
        latest: { ...snapshot.latest, [event.drone_id]: event },
        trails: { ...snapshot.trails, [event.drone_id]: trail },
      }
      notify()
      return true
    },
    reset() {
      if (snapshot === EMPTY) return
      snapshot = EMPTY
      notify()
    },
  }
}

/** Subscribes a component to the store; the snapshot identity changes only when a scan is kept. */
export function useSensorStore(store: SensorStore): SensorSnapshot {
  return useSyncExternalStore(store.subscribe, store.snapshot, store.snapshot)
}
