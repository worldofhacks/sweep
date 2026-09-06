import { useMemo, useState } from 'react'
import './live.css'
import { capabilityBlockedReason, isIntentEnabled } from '../../control/state'
import { Pane } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { FocusFeed } from './FocusFeed'
import { Mosaic } from './Mosaic'
import { useSecondTick } from './use-second-tick'

/** One dynamic wall, with local device inspection opened from a tile. */
export function LiveModule({ controller, now, media }: ModuleProps) {
  const [inspecting, setInspecting] = useState(false)
  const { state, selectFeed, toggleAircraft } = controller
  const devices = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  useSecondTick(devices.some((drone) => drone.video?.last_frame_at != null))
  const currentNow = now()
  const focused =
    state.selectedFeedId === null ? null : (state.aircraft[state.selectedFeedId] ?? null)

  return (
    <Pane
      title={inspecting ? 'Device inspection' : 'All devices'}
      note={inspecting
        ? 'Detailed camera and relay state. Return to All devices to see the full wall.'
        : 'All reported cameras in one wall. Focus a device to inspect its feed.'}
    >
      {inspecting ? (
        <>
          <button type="button" className="lv-focus lv-back" onClick={() => setInspecting(false)}>
            Back to All devices
          </button>
          <FocusFeed focused={focused} requests={state.requests} now={currentNow} media={media} />
        </>
      ) : devices.length === 0 ? (
        <EmptyModule
          what="camera sources"
          detail="No devices have joined this session. Their cameras appear here as they join."
        />
      ) : (
        <Mosaic
          devices={devices}
          now={currentNow}
          focusedId={focused?.drone_id ?? null}
          selection={state.selection}
          selectionEnabled={isIntentEnabled(state, 'select')}
          selectionDisabledReason={capabilityBlockedReason(state, 'select')}
          onFocus={(droneId) => {
            selectFeed(droneId)
            setInspecting(true)
          }}
          onToggleSelection={toggleAircraft}
          media={media}
        />
      )}
    </Pane>
  )
}
