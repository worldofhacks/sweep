import type { ComponentType } from 'react'
import type { useControlConsole } from '../control/use-control-console'

/** Everything the hook returns: authoritative state plus the intent functions. */
export type ConsoleController = ReturnType<typeof useControlConsole>

export type ModuleId =
  | 'control'
  | 'live'
  | 'gesture'
  | 'speech'
  | 'library'
  | 'builder'
  | 'reference'

export interface ModuleProps {
  controller: ConsoleController
  now: () => number
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
