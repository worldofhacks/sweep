import { useState } from 'react'
import './control.css'
import { Pane, type PaneTab } from '../../shell/Pane'
import type { ModuleProps } from '../types'
import { CapturePane } from './CapturePane'
import { CommandsPane } from './CommandsPane'
import { FleetPane } from './FleetPane'
import { RequestsPane } from './RequestsPane'
import { SwarmPane } from './SwarmPane'
import type { CaptureReadiness } from './controls'

export type ControlPaneId = 'swarm' | 'capture' | 'commands' | 'requests' | 'fleet'

const PANES: PaneTab[] = [
  { id: 'swarm', label: 'Swarm' },
  { id: 'capture', label: 'Capture' },
  { id: 'commands', label: 'Commands' },
  { id: 'requests', label: 'Requests' },
  { id: 'fleet', label: 'Fleet' },
]

export interface ControlModuleProps extends ModuleProps {
  /**
   * capture_readiness guidance for the compass and gates. No relay event on
   * main carries it yet, so the module renders it as unreported by default.
   */
  guidance?: CaptureReadiness | null
  initialPane?: ControlPaneId
}

/**
 * Control and capture: Swarm, Capture, Commands, Requests and Fleet on the
 * authoritative state. The room identifier lives in the shell because the
 * gesture producer and the speech compiler draft against the same room.
 */
export function ControlModule({
  controller,
  now,
  roomId,
  onRoomIdChange,
  guidance = null,
  initialPane = 'swarm',
}: ControlModuleProps) {
  const [pane, setPane] = useState<ControlPaneId>(initialPane)
  const [steps, setSteps] = useState(2)
  const [formationPreview, setFormationPreview] = useState<string | null>(null)

  return (
    <Pane
      title="Control and capture"
      note="Preview every request in full, then confirm."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as ControlPaneId)}
      tabsLabel="Control panes"
    >
      {pane === 'swarm' && (
        <SwarmPane
          controller={controller}
          steps={steps}
          onSteps={setSteps}
          formationPreview={formationPreview}
          onFormationPreview={setFormationPreview}
        />
      )}
      {pane === 'capture' && (
        <CapturePane controller={controller} roomId={roomId} onRoomId={onRoomIdChange} guidance={guidance} />
      )}
      {pane === 'commands' && <CommandsPane controller={controller} steps={steps} onSteps={setSteps} />}
      {pane === 'requests' && <RequestsPane controller={controller} />}
      {pane === 'fleet' && <FleetPane controller={controller} now={now} />}
    </Pane>
  )
}
