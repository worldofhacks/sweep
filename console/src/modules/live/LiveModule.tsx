import { useMemo, useState } from 'react'
import './live.css'
import { capabilityBlockedReason, isIntentEnabled } from '../../control/state'
import { Pane, type PaneTab } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { FocusFeed } from './FocusFeed'
import { Mosaic } from './Mosaic'
import { GROUND_WALL_SIZE, groundNote, mosaicNote } from './derive-live'
import { useSecondTick } from './use-second-tick'

type LivePane = 'all' | 'wall4' | 'wall6' | 'ground' | 'focus'

const PANES: PaneTab[] = [
  { id: 'all', label: 'All devices' },
  { id: 'wall4', label: 'Wall of 4' },
  { id: 'wall6', label: 'Wall of 6' },
  { id: 'ground', label: 'Ground' },
  { id: 'focus', label: 'Focus feed' },
]

/**
 * The Live surface from the Sweep Console v4 design: two aircraft walls, the
 * ground vehicle wall, and the focus feed. Focus is the reducer's
 * selectedFeedId, so it follows a single selection, survives video loss, and
 * clears only when the device leaves.
 */
export function LiveModule({ controller, now, media }: ModuleProps) {
  const [pane, setPane] = useState<LivePane>('all')
  const { state, selectFeed, toggleAircraft } = controller
  const devices = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  const aircraft = useMemo(
    () => devices.filter((device) => device.device_class === 'aircraft'),
    [devices],
  )
  const ground = useMemo(
    () => devices.filter((device) => device.device_class === 'ground_vehicle'),
    [devices],
  )
  useSecondTick(devices.some((drone) => drone.video?.last_frame_at != null))
  const currentNow = now()
  const focused =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)
  const wallProps = {
    now: currentNow,
    focusedId: focused?.drone_id ?? null,
    selection: state.selection,
    selectionEnabled: isIntentEnabled(state, 'select'),
    selectionDisabledReason: capabilityBlockedReason(state, 'select'),
    onFocus: selectFeed,
    onToggleSelection: toggleAircraft,
    media,
  }

  return (
    <Pane
      title="Live view"
      note="Every reported camera source with its focus pane. Detections are not reported yet."
      tabs={PANES}
      activeTab={pane}
      onTabChange={(id) => setPane(id as LivePane)}
      tabsLabel="Live panes"
    >
      {devices.length === 0 ? (
        <EmptyModule
          what="camera sources"
          detail="No devices have joined this session, so there is no wall and nothing to focus."
        />
      ) : pane === 'focus' ? (
        <FocusFeed focused={focused} requests={state.requests} now={currentNow} media={media} />
      ) : pane === 'all' ? (
        <Mosaic
          devices={devices}
          label="All devices"
          note={`${devices.length} reported ${devices.length === 1 ? 'device' : 'devices'}. New devices appear automatically; offline feeds keep their place.`}
          noun="device"
          {...wallProps}
        />
      ) : pane === 'ground' ? (
        ground.length === 0 ? (
          <EmptyModule
            what="ground vehicle camera sources"
            detail="No robots have joined this session. Ground tiles appear as ground vehicles join."
          />
        ) : (
          <Mosaic
            devices={ground}
            count={GROUND_WALL_SIZE}
            label="Ground"
            note={groundNote(ground.length)}
            noun="robot"
            {...wallProps}
          />
        )
      ) : (
        <Mosaic
          devices={aircraft}
          count={pane === 'wall6' ? 6 : 4}
          label={`Wall of ${pane === 'wall6' ? 6 : 4}`}
          note={mosaicNote(pane === 'wall6' ? 6 : 4, aircraft.length)}
          noun="aircraft"
          {...wallProps}
        />
      )}
    </Pane>
  )
}
