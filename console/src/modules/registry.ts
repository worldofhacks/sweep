import { FleetContext } from './FleetContext'
import { SpacesModule } from '../atlas/SpacesModule'
import { CapturesModule } from './captures/CapturesModule'
import { ControlModule } from './control/ControlModule'
import { DevicesModule } from './devices/DevicesModule'
import { GestureModule } from './gesture/GestureModule'
import { LiveModule } from './live/LiveModule'
import { MapModule } from './map/MapModule'
import { SpeechModule } from './speech/SpeechModule'
import { SearchModule } from './search/SearchModule'
import { WorldsModule } from './worlds/WorldsModule'
import type { ModuleDefinition, ModuleId } from './types'

/** Shared desktop and phone navigation order; Spaces and the fleet pages use one shell. */
export const MODULES: readonly ModuleDefinition[] = [
  {
    id: 'spaces', label: 'Spaces', title: 'Collaborative atlas',
    note: 'Local stories, real perspectives, one evolving atlas.',
    component: SpacesModule, context: FleetContext,
  },
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
    id: 'search',
    label: 'Search',
    title: 'Visual search',
    note: 'Preview the configured route, confirm the exact mission, then review coverage and findings.',
    component: SearchModule,
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
    id: 'map',
    label: 'Map',
    title: 'Fleet map',
    note: 'Reported device positions, LiDAR returns, and the relay occupancy map.',
    component: MapModule,
    context: FleetContext,
  },
]

export function getModule(id: ModuleId): ModuleDefinition {
  const module = MODULES.find((entry) => entry.id === id)
  if (!module) throw new Error(`Unknown module ${id}`)
  return module
}
