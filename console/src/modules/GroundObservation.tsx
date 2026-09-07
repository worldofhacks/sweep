import type { RelayAircraftState } from '../relay/contract'
import { DEVICE_FRESH_MS } from '../control/observation'
import { formatDeviceId } from '../control/state'
import { formatAgo, formatPercent } from '../shell/format'

export function GroundObservation({ device, now }: { device: RelayAircraftState; now: number }) {
  const ground = device.client_observation?.ground
  if (device.node_type !== 'ground' || !ground) return null
  const telemetry = ground.telemetry?.payload.kind === 'telemetry' ? ground.telemetry.payload : null
  const scan = ground.scan?.payload.kind === 'range_scan' ? ground.scan.payload : null
  const pose = ground.pose
  const reportNow = device.client_observation?.now ?? now
  const current = (t: number | undefined) => t !== undefined && device.client_observation?.connectionCurrent !== false &&
    !['leaving', 'disconnected'].includes(device.membership) && reportNow >= t && reportNow - t <= DEVICE_FRESH_MS
  const ranges = scan?.ranges_m.filter((range): range is number => range !== null) ?? []
  return (
    <div className="ground-observation" aria-label={`${formatDeviceId(device)} ground observations`}>
      <p>Authenticated ground observations · wire ID {device.drone_id} · epoch {device.connection_epoch}</p>
      <p>Last relay receipt {ground.lastReportAt === null ? 'unreported' : formatAgo(reportNow, ground.lastReportAt)}</p>
      <p>Declared pose source <code>{device.ground_readiness?.source_id ?? 'unreported'}</code> · {pose ? `${Math.round(pose.confidence * 100)}% confidence` : 'no accepted pose'} · {current(pose?.t_ingest) && ground.poseCurrent ? 'current pose report' : 'current pose unavailable'}</p>
      {telemetry && <p>{current(ground.telemetry?.t_ingest) ? 'Current telemetry' : 'Last reported telemetry · stale'} · battery {formatPercent(telemetry.battery)} · link {formatPercent(telemetry.link)} · quality {formatPercent(telemetry.pos_quality)} · state {telemetry.state}</p>}
      {telemetry && <p>Local odometry · {telemetry.position.frame} · x {telemetry.position.x_m.toFixed(2)} m · y {telemetry.position.y_m.toFixed(2)} m · z {telemetry.position.z_m.toFixed(2)} m</p>}
      <p>{scan ? `${current(ground.scan?.t_ingest) ? 'Current' : 'Last reported · stale'} LiDAR · ${ground.scan?.frame} · ${ranges.length}/${scan.ranges_m.length} returns${ranges.length ? ` · closest ${Math.min(...ranges).toFixed(2)} m` : ''}` : 'LiDAR scan unreported'}</p>
      <p>Local observations do not establish a world position or enable motion.</p>
    </div>
  )
}
