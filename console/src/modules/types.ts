import type { ComponentType } from 'react'
import type { useControlConsole } from '../control/use-control-console'
import type { GestureProducerDependencies } from '../gesture/use-gesture-producer'
import type { TranscriptClient } from '../voice/client'
import type { UsePushToTalkOptions } from '../voice/use-push-to-talk'

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
  transcript?: TranscriptClient
  gesture?: GestureProducerDependencies
  voice?: VoiceDependencies
}

export interface ModuleProps {
  controller: ConsoleController
  now: () => number
  /** Room identifier shared by Control › Capture, the gesture producer and the speech compiler. */
  roomId: string
  onRoomIdChange: (roomId: string) => void
  services: ModuleServices
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
