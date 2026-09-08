import { observationCurrent } from '../control/observation'
import type { RelayAircraftState, RelaySensorEvent } from '../relay/contract'

/** Display freshness only; scans are never a clearance or obstacle-coverage guarantee. */
export const SENSOR_FRESH_MS = 2_000
export function isFreshScan(scan: RelaySensorEvent, now: number): boolean {
  return now >= scan.t && now - scan.t <= SENSOR_FRESH_MS
}

export function sensorStatus(device: RelayAircraftState, now: number): { text: string; tone: string } {
  now = device.client_observation?.now ?? now
  if (!observationCurrent(device)) return { text: 'LiDAR current state unknown · device observation unavailable', tone: 'warn' }
  if (!device.adapter_capabilities.includes('lidar')) return { text: 'No lidar fitted / advertised · obstacle coverage unavailable', tone: 'warn' }
  const at = device.sensor?.last_scan_at
  if (at == null) return { text: 'Lidar advertised · no scan reported · coverage unknown', tone: 'warn' }
  const age = now - at
  if (age < 0) return { text: 'Lidar timestamp ahead of console · freshness unknown', tone: 'warn' }
  if (age > SENSOR_FRESH_MS) return { text: `Lidar stale · ${Math.floor(age / 1000)}s ago · coverage unknown`, tone: 'warn' }
  return { text: 'Lidar live · single scan plane only', tone: 'ink' }
}
