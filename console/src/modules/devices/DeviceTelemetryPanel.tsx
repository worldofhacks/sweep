import type { RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import { formatDeviceId } from '../../control/state'
import { deviceTelemetryRows, displayValue, humanLabel } from './telemetry'
import './telemetry.css'
import { RawLidarPlot } from './RawLidarPlot'

/** Same device facts in Devices, selection context and Live focus; the full report is always inspectable. */
export function DeviceTelemetryPanel({ device, now, scan, compact = false }: {
  device: RelayAircraftState; now: number; scan?: RelaySensorEvent | null; compact?: boolean
}) {
  const { client_observation, ...reported } = device
  const rows = deviceTelemetryRows(device, now)
  const custom = device.node_status?.device_telemetry
  const body = <>
    {client_observation && client_observation.state !== 'current' && <p className="tone-warn">{client_observation.reason} Values below are last reported.</p>}
    {!scan && <RawLidarPlot device={device} now={now} />}
    <dl className="tm-rows">
      {rows.map((row) => <div className="tm-row" key={row.label}><dt>{row.label}</dt><dd className={`tone-${row.tone ?? 'ink'}`}>{row.value}</dd></div>)}
    </dl>
    <details className="tm-details">
      <summary>Custom device telemetry {custom ? `· ${Object.keys(custom).length} groups` : '· unreported'}</summary>
      {custom ? Object.entries(custom).map(([group, value]) => <details className="tm-group" key={group}>
        <summary>{humanLabel(group)}</summary>
        <dl className="tm-rows">{(value && typeof value === 'object' && !Array.isArray(value) ? Object.entries(value) : [[group, value]]).map(([key, item]) =>
          <div className="tm-row" key={String(key)}><dt>{humanLabel(String(key))}</dt><dd>{displayValue(item)}</dd></div>)}</dl>
      </details>) : <p>No custom telemetry has been reported for this connection.</p>}
    </details>
    <details className="tm-details">
      <summary>Complete reported telemetry · {formatDeviceId(device)}</summary>
      <p>Last reported values for epoch {device.connection_epoch}. Missing fields are unreported. Readiness and command outcomes come from the relay.</p>
      <pre data-scroll="1" aria-label={`${formatDeviceId(device)} complete telemetry`}>{JSON.stringify({ ...reported, ...(scan ? { latest_scan: scan } : {}) }, null, 2)}</pre>
    </details>
  </>
  return compact ? <details className="tm-panel tm-details"><summary>Device telemetry and diagnostics</summary>{body}</details>
    : <section className="tm-panel" aria-label={`${formatDeviceId(device)} telemetry and diagnostics`}>{body}</section>
}
