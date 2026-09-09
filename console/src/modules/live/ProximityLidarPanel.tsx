import type { RelayAircraftState } from '../../relay/contract'
import { formatDeviceId } from '../../control/state'
import { reportAge } from '../devices/telemetry'

/**
 * The lidar mount sits ~0.286 m from the robot's turning center
 * (SWEEP_LIDAR_MOUNT_X_M=-0.268, Y=0.0999), rounded up: a return this close
 * can be the sensor's own mounting structure, not an external obstacle.
 * The relay does not publish the deployed footprint/stopping/clearance
 * config, so CAUTION_M is a multiple of that structural radius rather than
 * a live-configured stopping distance.
 */
const NEAR_M = 0.3
const CAUTION_M = 0.9

type Tier = 'near' | 'caution' | 'clear'

function tierFor(distance: number): Tier {
  if (distance <= NEAR_M) return 'near'
  if (distance <= CAUTION_M) return 'caution'
  return 'clear'
}

const TIER_RADIUS_PX: Record<Tier, number> = { near: 3, caution: 2.2, clear: 1.4 }
const TIER_WORD: Record<Tier, string> = { near: 'near · ≤0.3m', caution: 'caution · ≤0.9m', clear: 'clear · >0.9m' }

/**
 * The live lidar scan beside the video, colour- and size-coded by distance.
 * Self-return masking is not wired for any deployed adapter (no runtime
 * flag sets a qualified SelfReturnProfile), so the nearest points may be the
 * robot's own body; this panel says so rather than presenting them as a
 * confirmed hazard. Never enters the sensor/map store.
 */
export function ProximityLidarPanel({ device, now }: { device: RelayAircraftState; now: number }) {
  now = device.client_observation?.now ?? now
  const label = formatDeviceId(device)
  const observation = device.client_observation?.ground?.scan
  const hasScan = observation?.payload.kind === 'range_scan' && observation.connection_epoch === device.connection_epoch
  if (!hasScan) {
    return (
      <figure className="lv-lidar" aria-label={`${label} proximity LiDAR`}>
        <div className="lv-lidar-empty">No LiDAR scan reported for this device.</div>
      </figure>
    )
  }
  const scan = observation.payload
  const age = now - observation.t_ingest
  const current = device.client_observation?.connectionCurrent !== false && age >= 0 && age <= 1000 &&
    !['disconnected', 'leaving'].includes(device.membership)
  const total = scan.ranges_m.length
  const valid = scan.ranges_m
    .map((range, index) => ({ range, index }))
    .filter((entry): entry is { range: number; index: number } =>
      entry.range !== null && entry.range >= scan.range_min_m && entry.range <= scan.range_max_m)
  const coveragePct = total > 0 ? Math.round((valid.length / total) * 100) : 0
  const closest = valid.length > 0 ? Math.min(...valid.map((entry) => entry.range)) : null
  const scale = Math.max(CAUTION_M * 1.2, ...valid.map((entry) => entry.range))

  return (
    <figure className="lv-lidar" aria-label={`${label} proximity LiDAR`}>
      <svg viewBox="0 0 128 128" width="128" height="128" role="img" aria-label={`${label} LiDAR returns coloured by distance`}>
        <path d="M64 64V8" stroke="currentColor" opacity="0.3" />
        {([NEAR_M, CAUTION_M] as const).map((ringM) => (
          <circle key={ringM} cx="64" cy="64" r={Math.min(56, (ringM / scale) * 56)} fill="none" stroke="currentColor" opacity="0.25" strokeDasharray="2 2" />
        ))}
        {current && scan.ranges_m.map((range, index) => {
          const angle = scan.angle_min_rad + index * scan.angle_increment_rad
          const dx = -Math.sin(angle)
          const dy = -Math.cos(angle)
          if (range === null || range < scan.range_min_m || range > scan.range_max_m) {
            return <line key={index} x1={64 + dx * 54} y1={64 + dy * 54} x2={64 + dx * 56} y2={64 + dy * 56}
              stroke="currentColor" opacity="0.35" strokeWidth="1" />
          }
          const tier = tierFor(range)
          const r = (range / scale) * 56
          return <circle key={index} cx={64 + dx * r} cy={64 + dy * r} r={TIER_RADIUS_PX[tier]}
            className={`lv-lidar-pt is-${tier}`} />
        })}
      </svg>
      <div className="lv-lidar-side">
        <p className="lv-lidar-status">{current ? 'Receiving scans' : 'Scan stale or disconnected'}</p>
        <ul className="lv-lidar-legend">
          {(['near', 'caution', 'clear'] as const).map((tier) => (
            <li key={tier} className={`is-${tier}`}><span className="lv-lidar-swatch" aria-hidden="true" />{TIER_WORD[tier]}</li>
          ))}
        </ul>
        <p className="lv-lidar-note">
          {valid.length}/{total} bearings returned ({coveragePct}%) · closest {closest === null ? 'none' : `${closest.toFixed(2)}m`}
          {' · '}received {reportAge(observation.t_ingest, now)}
        </p>
        {coveragePct < 100 && (
          <p className="lv-lidar-note is-warn">
            {100 - coveragePct}% of bearings returned no echo. Those directions are unknown, not clear.
          </p>
        )}
        <p className="lv-lidar-note is-warn">
          Self-return filtering is not enabled on any deployed adapter. A close point near the
          mount offset may be the robot&apos;s own body, not an obstacle.
        </p>
      </div>
    </figure>
  )
}
