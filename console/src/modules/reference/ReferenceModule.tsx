import { useState } from 'react'
import { Pane, type PaneTab } from '../../shell/Pane'
import { ConfigModule } from '../config/ConfigModule'
import { ConnectivityModule } from '../connectivity/ConnectivityModule'
import { MissionTracker } from '../control/MissionTracker'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { StatesGallery } from './StatesGallery'

type ReferenceTab = 'mission' | 'health' | 'config' | 'ledger' | 'map' | 'gallery'

const TABS: PaneTab[] = [
  { id: 'mission', label: 'Mission' },
  { id: 'health', label: 'Health' },
  { id: 'config', label: 'Config' },
  { id: 'ledger', label: 'Ledger' },
  { id: 'map', label: 'Map' },
  { id: 'gallery', label: 'States' },
]

const SECTIONS: Record<ReferenceTab, { title: string; note: string; what: string }> = {
  mission: {
    title: 'Scripted mission',
    note: 'Appendix E: ten steps, hands-free, under three minutes.',
    what: 'a mission tracker',
  },
  health: {
    title: 'Connectivity and health',
    note: 'Nodes, services, metrics, and the degradation ladder.',
    what: 'per-node health or shared-service status',
  },
  config: {
    title: 'Configuration',
    note: 'Ordinary settings apply now; safety-sensitive ones are staged.',
    what: 'editable configuration',
  },
  ledger: {
    title: 'Ledger and replay',
    note: 'Hash-chained session log, replayable by id.',
    what: 'a session ledger or replay',
  },
  map: {
    title: 'Map and room graph',
    note: 'Positions, doorways, capture poses, geofence.',
    what: 'positions or a room graph',
  },
  gallery: {
    title: 'States gallery',
    note: 'Every vocabulary value, as the console renders it.',
    what: 'a states gallery',
  },
}

/**
 * The Reference group: Mission is the Appendix E tracker, Health is the
 * Connectivity module, Config is the Configuration module, States is the
 * vocabulary gallery. Ledger and Map stay honest empties until the relay feeds
 * them.
 */
export function ReferenceModule(props: ModuleProps) {
  const [tab, setTab] = useState<ReferenceTab>('mission')
  const section = SECTIONS[tab]
  return (
    <Pane
      title="Reference"
      note={`${section.title} — ${section.note}`}
      tabs={TABS}
      activeTab={tab}
      onTabChange={(id) => setTab(id as ReferenceTab)}
      tabsLabel="Reference sections"
      tabsVariant="reference"
    >
      {tab === 'mission' ? (
        <MissionTracker now={props.now} />
      ) : tab === 'health' ? (
        <ConnectivityModule {...props} />
      ) : tab === 'config' ? (
        <ConfigModule {...props} />
      ) : tab === 'gallery' ? (
        <StatesGallery />
      ) : (
        <EmptyModule what={section.what} />
      )}
    </Pane>
  )
}
