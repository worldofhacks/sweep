import { useMemo, useState } from 'react'
import './live.css'
import { capabilityBlockedReason, isIntentEnabled } from '../../control/state'
import { Pane, type PaneTab } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { FocusFeed } from './FocusFeed'
import { Mosaic } from './Mosaic'
import { useSecondTick } from './use-second-tick'

type LivePane = 'wall4' | 'wall6' | 'focus'

const PANES: PaneTab[] = [
  { id: 'wall4', label: 'Wall of 4' },
  { id: 'wall6', label: 'Wall of 6' },
  { id: 'focus', label: 'Focus feed' },
]

/**
 * The Live surface from the Sweep Console v4 design: two walls and the focus
 * feed. Focus is the reducer's selectedFeedId, so it follows a single
 * selection, survives video loss, and clears only when the aircraft leaves.
 */
export function LiveModule({ controller, now, media }: ModuleProps) {
  const [pane, setPane] = useState<LivePane>('wall4')
  const { state, selectFeed, toggleAircraft } = controller
  const aircraft = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  useSecondTick(aircraft.some((drone) => drone.video?.last_frame_at != null))
  const currentNow = now()
  const focused =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)

  return (
    <Pane
      title="Live view"
      note="Every reported camera source with its focus pane. Detections are not reported yet."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as LivePane)}
      tabsLabel="Live panes"
    >
      {aircraft.length === 0 ? (
        <EmptyModule
          what="camera sources"
          detail="No aircraft have joined this session, so there is no wall and nothing to focus."
        />
      ) : pane === 'focus' ? (
        <FocusFeed focused={focused} requests={state.requests} now={currentNow} media={media} />
      ) : (
        <Mosaic
          aircraft={aircraft}
          count={pane === 'wall6' ? 6 : 4}
          now={currentNow}
          focusedId={focused?.drone_id ?? null}
          selection={state.selection}
          selectionEnabled={isIntentEnabled(state, 'select')}
          selectionDisabledReason={capabilityBlockedReason(state, 'select')}
          onFocus={selectFeed}
          onToggleSelection={toggleAircraft}
          media={media}
        />
      )}
    </Pane>
  )
}
