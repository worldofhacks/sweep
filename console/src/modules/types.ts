import type { NavigationClient } from '../navigation'
import type { SearchClient } from '../search/client'
import type { MapAuthoringClient } from './map/authoring/client'
import type { ComponentType } from 'react'
import type { CatalogController } from '../catalog/use-catalog'
import type { useControlConsole } from '../control/use-control-console'
import type { GestureProducerDependencies } from '../gesture/use-gesture-producer'
import type { MediaRuntime } from '../media/runtime'
import type { MapEndpoint } from '../relay/map-endpoint'
import type { TranscriptClient } from '../voice/client'
import type { UsePushToTalkOptions } from '../voice/use-push-to-talk'

/** Everything the hook returns: authoritative state plus the intent functions. */
export type ConsoleController = ReturnType<typeof useControlConsole>

export type ModuleId =
  | 'control'
  | 'live'
  | 'gesture'
  | 'speech'
  | 'search'
  | 'captures'
  | 'worlds'
  | 'devices'
  | 'map'

/** Browser seams for the push-to-talk recorder; tests inject fakes. */
export type VoiceDependencies = Pick<
  UsePushToTalkOptions,
  'requestAudio' | 'recorderFactory' | 'nextId' | 'maxRecordingMs' | 'now'
>

/**
 * Input services the modules bind to. Absent members are honest absences: no
 * transcript client means the relay has no transcription endpoint here.
 */
export interface ModuleServices {
  /** Accepted-map review port; absent until a real provider is connected. */
  navigation?: NavigationClient
  search?: SearchClient
  /** Explicit saved-map integration; absent backend actions remain unavailable. */
  mapAuthoring?: MapAuthoringClient
  transcript?: TranscriptClient
  gesture?: GestureProducerDependencies
  voice?: VoiceDependencies
}

export interface ModuleProps {
  controller: ConsoleController
  /** Captures, worlds, node details, and configuration; unreported in production. */
  catalog: CatalogController
  now: () => number
  /** Room identifier shared by Control › Capture, the gesture producer and the speech compiler. */
  roomId: string
  onRoomIdChange: (roomId: string) => void
  services: ModuleServices
  /** Playback runtime; absent until the media bootstrap provides a configuration. */
  media?: MediaRuntime
  /** Relay WebSocket base URL from the bootstrap; absent in the fixture and without a bootstrap. */
  relayBaseUrl?: string
  /** The session's occupancy map behind the relay bearer; absent means the map cannot be read. */
  mapEndpoint?: MapEndpoint
}

export interface ModuleDefinition {
  id: ModuleId
  /** Navigation label in the rail and the tab bar. */
  label: string
  /** Working-pane heading. */
  title: string
  /** Working-pane note under the heading. */
  note: string
  component: ComponentType<ModuleProps>
  /** Rendered inside the context column while this module is active. */
  context: ComponentType<ModuleProps>
}
