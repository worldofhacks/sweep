import { useState } from 'react'
import { Pane } from '../../shell/Pane'
import type { ModuleProps } from '../types'
import { FleetMap } from './FleetMap'
import { MapAuthoring } from './authoring/MapAuthoring'

/** Live observations and saved-map editing share the existing Map module. */
export function MapModule(props: ModuleProps) {
  const [pane, setPane] = useState('live')
  const [authoringOpened, setAuthoringOpened] = useState(false)
  return <Pane preserveContents title="Fleet map" note="View reported observations or author a shared map from measured inputs."
    tabs={[{ id: 'live', label: 'Live observations' }, { id: 'author', label: 'Map authoring' }]}
    activeTab={pane} tabsLabel="Map panes" onTabChange={(id) => {
      setPane(id)
      if (id === 'author') setAuthoringOpened(true)
    }}>
    {pane === 'live' && <FleetMap {...props} />}
    {authoringOpened && <div hidden={pane !== 'author'}>
      <p className="mp-status">Export your local draft before leaving Map or closing the console.</p>
      <MapAuthoring client={props.services.mapAuthoring} now={props.now} state={props.controller.state} />
    </div>}
  </Pane>
}
