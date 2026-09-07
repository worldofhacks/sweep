import { groundControlBlockedReason, groundPulseArgs, type GroundDirection } from '../../control/ground'
import { formatDeviceId } from '../../control/state'
import type { ModuleProps } from '../types'

export function GroundPane({ controller }: Pick<ModuleProps, 'controller'>) {
  const state = controller.state
  const device = state.selection.length === 1 ? state.aircraft[state.selection[0]] : undefined
  const reason = groundControlBlockedReason(state, 'ground_velocity')
  const returnReason = groundControlBlockedReason(state, 'come_home') ?? (device?.node_type === 'ground' ? null : 'Select exactly one ground robot.')
  return <section aria-label="Ground controls">
    <h3>Ground robot</h3>
    <p>{device?.node_type === 'ground' ? `${formatDeviceId(device)} · wire ID ${device.drone_id} · connection epoch ${device.connection_epoch}` : 'Select exactly one ready ground robot in Fleet.'}</p>
    <p>Preview a 250 ms pulse, then confirm its exact parameters. Requested speeds are not measured distance or turn guarantees. The relay checks the configured clearance and operator conditions.</p>
    <div className="ct-row">{(['forward', 'left', 'right'] as GroundDirection[]).map((direction) => <button type="button" key={direction} disabled={reason !== null}
      onClick={() => controller.prepareIntent({ name: 'ground_velocity', args: groundPulseArgs(direction) })}>
      {direction === 'forward' ? 'Forward pulse' : `Turn ${direction}`} · {direction === 'forward' ? '80 mm/s' : '350 mrad/s'}
    </button>)}</div>
    {reason && <p role="status">{reason}</p>}
    <button type="button" disabled={returnReason !== null} onClick={() => controller.prepareIntent({ name: 'come_home', args: {} })}>Preview configured ground return</button>
    <p>{returnReason ?? 'Return requires a separately approved route configured on the relay. The console supplies no route or return identifier.'}</p>
    <button type="button" onClick={() => controller.issueHold()}>HOLD selected devices</button>
  </section>
}
