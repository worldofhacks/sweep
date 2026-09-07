import type { RelayAircraftState } from '../relay/contract'
import { formatAgo, formatPercent } from '../shell/format'

export function GroundObservation({ device, now }: { device: RelayAircraftState; now: number }) {
  const ground = device.client_observation?.ground
  if (device.node_type !== 'ground' || !ground) return null
  const telemetry = ground.telemetry?.payload.kind === 'telemetry' ? ground.telemetry.payload : null
  const pose = ground.pose
  return (
    <div className="ground-observation" aria-label={`${device.drone_id} ground observations`}>
      <p>Signed observations · epoch {device.connection_epoch}</p>
      <p>Last relay receipt {ground.lastReportAt === null ? 'unreported' : formatAgo(now, ground.lastReportAt)}</p>
      <p>Declared pose source <code>{device.ground_readiness?.source_id ?? 'unreported'}</code> · {pose ? `${Math.round(pose.confidence * 100)}% confidence` : 'no accepted pose'}</p>
      {telemetry && <p>Battery {formatPercent(telemetry.battery)} · link {formatPercent(telemetry.link)} · quality {formatPercent(telemetry.pos_quality)} · state {telemetry.state}</p>}
      {telemetry && <p>Local odometry · {telemetry.position.frame} · x {telemetry.position.x_m.toFixed(2)} m · y {telemetry.position.y_m.toFixed(2)} m · z {telemetry.position.z_m.toFixed(2)} m</p>}
    </div>
  )
}
