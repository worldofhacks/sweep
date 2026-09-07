import type { RelayAircraftState } from '../../relay/contract'
import { formatDeviceId } from '../../control/state'
import { telemetryGroup, reportAge } from './telemetry'

/** Raw bin indices have no asserted robot heading or world pose. Never enters the sensor/map store. */
export function RawLidarPlot({ device, now }: { device: RelayAircraftState; now: number }) {
  now = device.client_observation?.now ?? now
  const lidar = telemetryGroup(device, 'lidar')
  const ranges = lidar?.ranges_cm
  if (!Array.isArray(ranges) || ranges.length < 1 || ranges.length > 512 ||
    !ranges.every((range) => typeof range === 'number' && Number.isFinite(range) && range >= 0)) return null
  const values = ranges as number[]
  const scale = Math.max(100, ...values)
  const label = formatDeviceId(device)
  return <figure className="tm-raw-scan" aria-label={`${label} raw LiDAR sensor frame`}>
    <svg viewBox="0 0 128 128" width="128" height="128" role="img" aria-label={`${label} raw LiDAR returns by sensor bin`}>
      {[0.25, 0.5, 0.75, 1].map((fraction) => <circle key={fraction} cx="64" cy="64" r={fraction * 56} fill="none" stroke="currentColor" opacity="0.25" />)}
      <path d="M64 64V8" stroke="currentColor" opacity="0.3" />
      {values.map((range, index) => range > 0 ? <circle key={index}
        cx={64 - Math.sin(index / values.length * 2 * Math.PI) * range / scale * 56}
        cy={64 - Math.cos(index / values.length * 2 * Math.PI) * range / scale * 56} r="1" fill="currentColor" /> : null)}
    </svg>
    <figcaption>Raw sensor frame · bin 0 at top · {lidar?.calibrated === true ? 'calibration reported' : 'robot orientation uncalibrated or unreported'} · {values.filter((range) => range > 0).length}/{values.length} returns · {(scale / 100).toFixed(1)} m scale · {reportAge(device.node_status?.t, now)}. Not projected onto the world map.</figcaption>
  </figure>
}
