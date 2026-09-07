import { useState } from 'react'
import { cameraControlBlockedReason, cameraTelemetryGroup } from '../../control/camera'
import { formatDeviceId } from '../../control/state'
import type { CameraControlArgs, RelayAircraftState } from '../../relay/contract'
import type { ModuleProps } from '../types'
import { displayValue } from './telemetry'

export function CameraControls({ controller, device, now }: { controller: ModuleProps['controller']; device: RelayAircraftState; now: number }) {
  const [pitch, setPitch] = useState('0')
  if (device.device_class !== 'aircraft') return null
  const gimbal = cameraTelemetryGroup(device, 'gimbal')
  const camera = cameraTelemetryGroup(device, 'camera')
  const pitchMdeg = pitch.trim() === '' ? NaN : Number(pitch) * 1000
  const action = (label: string, args: CameraControlArgs) => {
    const reason = cameraControlBlockedReason(controller.state, device, args, now)
    return <div className="rp-action"><button type="button" disabled={reason !== null} title={reason ?? `Preview ${label.toLowerCase()} for ${formatDeviceId(device)}.`}
      onClick={() => controller.prepareIntent({ name: 'camera_control', args, targets: [device.drone_id] }, 'console')}>
      Preview {label.toLowerCase()}
    </button>{reason && <p className="tone-warn">{reason}</p>}</div>
  }
  return <details className="tm-details rp-controls"><summary>Camera controls · {formatDeviceId(device)}</summary>
    <p>Targets this aircraft. Review and confirm each camera action in the confirmation dock.</p>
    <fieldset><legend>Camera</legend>
      <p>Mode: {displayValue(camera?.mode)} · recording: {displayValue(camera?.recording)} · busy: {displayValue(camera?.busy)}</p>
      {action('Photo mode', { kind: 'ready' })}
      {action('Single photo', { kind: 'photo' })}
      <p>A completed photo requires a new file reported by the aircraft. Photos remain on its storage; downloading and panorama capture are unavailable in this adapter.</p>
    </fieldset>
    <fieldset><legend>Gimbal pitch</legend>
      <p>Measured: {displayValue(gimbal?.pitch_deg)}° · reported range: {displayValue(gimbal?.pitch_min_deg)}° to {displayValue(gimbal?.pitch_max_deg)}°</p>
      <label>Absolute pitch · degrees<input type="number" step="0.1" value={pitch}
        min={typeof gimbal?.pitch_min_deg === 'number' ? gimbal.pitch_min_deg : undefined}
        max={typeof gimbal?.pitch_max_deg === 'number' ? gimbal.pitch_max_deg : undefined}
        onChange={(event) => setPitch(event.target.value)} /></label>
      {action('Gimbal pitch', { kind: 'gimbal', pitch_mdeg: pitchMdeg })}
      <p>Completion requires a fresh measured pitch at the target.</p>
    </fieldset>
  </details>
}
