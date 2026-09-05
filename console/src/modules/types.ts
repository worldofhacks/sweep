import type { ComponentType } from 'react'
import type { CatalogController } from '../catalog/use-catalog'
import type { useControlConsole } from '../control/use-control-console'
import type { MediaRuntime } from '../media/runtime'

/** Everything the hook returns: authoritative state plus the intent functions. */
export type ConsoleController = ReturnType<typeof useControlConsole>

export type ModuleId =
  | 'control'
  | 'live'
  | 'gesture'
  | 'speech'
  | 'captures'
  | 'worlds'
  | 'reference'

export interface ModuleProps {
  controller: ConsoleController
  /** Captures, worlds, node details, and configuration; unreported in production. */
  catalog: CatalogController
  now: () => number
  /** Playback runtime; absent until the media bootstrap provides a configuration. */
  media?: MediaRuntime
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
