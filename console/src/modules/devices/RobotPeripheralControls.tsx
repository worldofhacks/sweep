import { useState } from 'react'
import { formatDeviceId } from '../../control/state'
import { peripheralBlockedReason } from '../../control/peripherals'
import { isRobotPeripheralArgs, type RelayAircraftState, type RobotPeripheralArgs } from '../../relay/contract'
import type { ModuleProps } from '../types'

/** Explicit connected-device targeting never changes or broadens the fleet motion selection. */
export function RobotPeripheralControls({ controller, device }: { controller: ModuleProps['controller']; device: RelayAircraftState }) {
  const [neck, setNeck] = useState(512)
  const [hsv, setHsv] = useState({ h: 0, s: 0, v: 40 })
  const [speech, setSpeech] = useState('')
  const [message, setMessage] = useState('')
  if (device.device_class !== 'ground_vehicle') return null
  const action = (label: string, args: RobotPeripheralArgs) => {
    const reason = peripheralBlockedReason(controller.state, device, args.kind)
    const invalid = !isRobotPeripheralArgs(args)
    return <div className="rp-action"><button type="button" disabled={Boolean(reason) || invalid}
      title={reason ?? (invalid ? 'Enter values within the displayed limits.' : `Preview ${label.toLowerCase()} for ${formatDeviceId(device)}.`)}
      onClick={() => controller.prepareIntent({ name: 'robot_peripheral', args, targets: [device.drone_id] }, 'console')}>
      Preview {label.toLowerCase()}
    </button>{reason && <p className="tone-warn">{reason}</p>}</div>
  }
  return <details className="tm-details rp-controls"><summary>Robot peripheral controls · {formatDeviceId(device)}</summary>
    <p>Targets this robot independently of the fleet motion selection. Every action opens an exact preview for confirmation.</p>
    <fieldset><legend>Neck tilt</legend>
      <label>Position · 300–650<input type="number" min="300" max="650" step="1" value={neck} onChange={(event) => setNeck(Number(event.target.value))} /></label>
      <p>512 is forward. Uses the vendor neck model; the neck must already be awake locally.</p>
      {action('Neck tilt', { kind: 'neck', position: neck })}
    </fieldset>
    <fieldset><legend>Base lights</legend><div className="rp-hsv">{(['h', 's', 'v'] as const).map((key) =>
      <label key={key}>{({ h: 'Hue', s: 'Saturation', v: 'Brightness' })[key]} · 0–255<input type="number" min="0" max="255" step="1" value={hsv[key]} onChange={(event) => setHsv({ ...hsv, [key]: Number(event.target.value) })} /></label>)}</div>
      {action('Base lights', { kind: 'lights', ...hsv })}
    </fieldset>
    <fieldset><legend>Robot speech</legend><label>Text to speak · up to 240 characters<textarea value={speech} maxLength={240} rows={2} onChange={(event) => setSpeech(event.target.value)} /></label>
      {action('Speech', { kind: 'speech', text: speech })}
    </fieldset>
    <fieldset><legend>Safety-page message</legend><label>Plain text · empty clears the message<textarea value={message} maxLength={240} rows={2} onChange={(event) => setMessage(event.target.value)} /></label>
      {action('Screen message', { kind: 'screen', text: message })}
      <p>Updates the local Sweep safety page. The STOP button and spotter controls remain available.</p>
    </fieldset>
    <p>Vendor acknowledgements confirm submission. Neck position, light output and spoken audio are not measured by this adapter.</p>
  </details>
}
