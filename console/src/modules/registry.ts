import { FleetContext } from './FleetContext'
import { CapturesModule } from './captures/CapturesModule'
import { ControlModule } from './control/ControlModule'
import { DevicesModule } from './devices/DevicesModule'
import { GestureModule } from './gesture/GestureModule'
import { LiveModule } from './live/LiveModule'
import { ReferenceModule } from './reference/ReferenceModule'
import { SpeechModule } from './speech/SpeechModule'
import { WorldsModule } from './worlds/WorldsModule'
import type { ModuleDefinition, ModuleId } from './types'

/** Navigation order: Control, Live, Gesture, Speech, Captures, Worlds, Devices, Reference. */
export const MODULES: readonly ModuleDefinition[] = [
  {
    id: 'control',
    label: 'Control',
    title: 'Control and capture',
    note: 'Preview every request in full, then confirm.',
    component: ControlModule,
    context: FleetContext,
  },
  {
    id: 'live',
    label: 'Live',
    title: 'Live view',
    note: 'Every reported camera source with its focus pane. Detections are not reported yet.',
    component: LiveModule,
    context: FleetContext,
  },
  {
    id: 'gesture',
    label: 'Gesture',
    title: 'Gesture recognition',
    note: 'Live camera in, canonical intents out. Tracking is off until you enable it.',
    component: GestureModule,
    context: FleetContext,
  },
  {
    id: 'speech',
    label: 'Speech',
    title: 'Speech to intents',
    note: 'An utterance compiles to intents, the arbiter validates, you confirm. Never a command straight to a device.',
    component: SpeechModule,
    context: FleetContext,
  },
  {
    id: 'captures',
    label: 'Captures',
    title: 'Capture library',
    note: 'Captured media by room, capture, device and time.',
    component: CapturesModule,
    context: FleetContext,
  },
  {
    id: 'worlds',
    label: 'Worlds',
    title: 'World Builder',
    note: 'Rooms, bundles, and generation jobs. A generated world is never a safety record.',
    component: WorldsModule,
    context: FleetContext,
  },
  {
    id: 'devices',
    label: 'Devices',
    title: 'Devices',
    note: 'Every device the relay reports, its class and feeds, and the configuration a node needs to join.',
    component: DevicesModule,
    context: FleetContext,
  },
  {
    id: 'reference',
    label: 'Reference',
    title: 'Reference',
    note: 'Mission, health, configuration, ledger, map, and the states gallery.',
    component: ReferenceModule,
    context: FleetContext,
  },
]

export function getModule(id: ModuleId): ModuleDefinition {
  const module = MODULES.find((entry) => entry.id === id)
  if (!module) throw new Error(`Unknown module ${id}`)
  return module
}
