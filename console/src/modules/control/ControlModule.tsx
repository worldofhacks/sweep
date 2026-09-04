import { useMemo, useState } from 'react'
import { Pane, type PaneTab } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { FleetRegistry } from '../FleetContext'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { ActiveAircraftPanel } from './ActiveAircraftPanel'
import { CapturePanel } from './CapturePanel'
import { RegistryPanel } from './RegistryPanel'
import { RequestsPanel } from './RequestsPanel'

type ControlPane = 'swarm' | 'capture' | 'commands' | 'requests' | 'fleet'

const PANES: PaneTab[] = [
  { id: 'swarm', label: 'Swarm' },
  { id: 'capture', label: 'Capture' },
  { id: 'commands', label: 'Commands' },
  { id: 'requests', label: 'Requests' },
  { id: 'fleet', label: 'Fleet' },
]

export function ControlModule({ controller }: ModuleProps) {
  const [pane, setPane] = useState<ControlPane>('swarm')
  const [roomId, setRoomId] = useState('room-01')
  const { state } = controller
  const aircraft = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  const activeAircraft = useMemo(() => {
    const selected = aircraft.filter((drone) => state.selection.includes(drone.drone_id))
    const remaining = aircraft.filter(
      (drone) =>
        !state.selection.includes(drone.drone_id) &&
        drone.membership !== 'disconnected' &&
        drone.membership !== 'leaving',
    )
    return [...selected, ...remaining].slice(0, 2)
  }, [aircraft, state.selection])

  return (
    <Pane
      title="Control and capture"
      note="Preview every request in full, then confirm."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as ControlPane)}
      tabsLabel="Control panes"
    >
      {pane === 'swarm' && (
        <div data-two="1">
          <RegistryPanel controller={controller} aircraft={aircraft} />
          <ActiveAircraftPanel activeAircraft={activeAircraft} selection={state.selection} />
        </div>
      )}
      {pane === 'capture' && (
        <div data-two="1">
          <CapturePanel controller={controller} roomId={roomId} onRoomIdChange={setRoomId} />
        </div>
      )}
      {pane === 'commands' && (
        <EmptyModule
          what="a command catalogue"
          detail="The console sends select, hold, capture_room and estop from the Swarm and Capture panes. The relay accepts no other intent name from this console yet, so no catalogue is rendered."
        />
      )}
      {pane === 'requests' && <RequestsPanel controller={controller} />}
      {pane === 'fleet' && <FleetRegistry controller={controller} layout="two" />}
    </Pane>
  )
}
