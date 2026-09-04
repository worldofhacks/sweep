import { FleetContext } from './FleetContext'
import { BuilderModule } from './builder/BuilderModule'
import { ControlModule } from './control/ControlModule'
import { GestureModule } from './gesture/GestureModule'
import { LibraryModule } from './library/LibraryModule'
import { LiveModule } from './live/LiveModule'
import { ReferenceModule } from './reference/ReferenceModule'
import { SpeechModule } from './speech/SpeechModule'
import type { ModuleDefinition, ModuleId } from './types'

/** Navigation order from the design: Control, Live, Gesture, Speech, Captures, Worlds, Reference. */
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
    note: 'An utterance compiles to intents, the arbiter validates, you confirm. Never a command straight to an aircraft.',
    component: SpeechModule,
    context: FleetContext,
  },
  {
    id: 'library',
    label: 'Captures',
    title: 'Capture library',
    note: 'Captured media by room, capture, aircraft and time.',
    component: LibraryModule,
    context: FleetContext,
  },
  {
    id: 'builder',
    label: 'Worlds',
    title: 'World Builder',
    note: 'Rooms, bundles, and generation jobs. A generated world is never a safety record.',
    component: BuilderModule,
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
