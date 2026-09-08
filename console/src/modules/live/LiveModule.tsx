import { useMemo, useState } from 'react'
import './live.css'
import { capabilityBlockedReason, isIntentEnabled } from '../../control/state'
import { Pane } from '../../shell/Pane'
import { sortedAircraft } from '../../shell/derive'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'
import { ambiguousCameraStreams, type CameraTarget } from '../../media/cameras'
import { CameraMosaic } from './CameraMosaic'
import { FocusFeed } from './FocusFeed'
import { Mosaic } from './Mosaic'
import { useSecondTick } from './use-second-tick'

/** One dynamic wall, with local device inspection opened from a tile. */
export function LiveModule(props: ModuleProps) {
  return <LiveSession key={props.controller.state.sessionId} {...props} />
}

function LiveSession({ controller, now, media }: ModuleProps) {
  const [inspecting, setInspecting] = useState(false)
  const [view, setView] = useState<'devices' | 'cameras'>('devices')
  const [camera, setCamera] = useState<CameraTarget | null>(null)
  const { state, selectFeed, toggleAircraft } = controller
  const devices = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  useSecondTick(devices.length > 0)
  const currentNow = now()
  const focusedId = inspecting && camera ? camera.deviceId : state.selectedFeedId
  const focused = focusedId === null ? null : (state.aircraft[focusedId] ?? null)
  const ambiguousStreams = useMemo(() => ambiguousCameraStreams(devices), [devices])
  const wallTitle = view === 'cameras' ? 'All cameras' : 'All devices'

  return (
    <Pane
      title={inspecting ? (camera ? 'Camera inspection' : 'Device inspection') : wallTitle}
      note={inspecting
        ? `Detailed camera and relay state. Return to ${wallTitle} to see the full wall.`
        : view === 'cameras' ? 'Every explicitly configured camera, with independent source and playback status.'
          : 'One tile per reported device. Choose All cameras to see each configured feed.'}
    >
      <div className="lv-view-switch" role="group" aria-label="Live view">
        {(['devices', 'cameras'] as const).map((option) => <button key={option} type="button"
          aria-pressed={view === option && !inspecting} onClick={() => { setView(option); setInspecting(false) }}>
          {option === 'cameras' ? 'All cameras' : 'All devices'}
        </button>)}
      </div>
      {inspecting ? (
        <>
          <button type="button" className="lv-focus lv-back" onClick={() => setInspecting(false)}>
            Back to {wallTitle}
          </button>
          <FocusFeed focused={focused} requests={state.requests} now={currentNow} media={media}
            target={camera} onTargetChange={setCamera} unavailableStreams={ambiguousStreams} />
        </>
      ) : devices.length === 0 ? (
        <EmptyModule
          what="camera sources"
          detail="No devices have joined this session. Their cameras appear here as they join."
        />
      ) : view === 'cameras' ? (
        <CameraMosaic devices={devices} now={currentNow} focused={camera} media={media}
          onFocus={(target) => { setCamera(target); selectFeed(target.deviceId); setInspecting(true) }} />
      ) : (
        <Mosaic
          devices={devices}
          now={currentNow}
          focusedId={focused?.drone_id ?? null}
          selection={state.selection}
          selectionEnabled={isIntentEnabled(state, 'select')}
          selectionDisabledReason={capabilityBlockedReason(state, 'select')}
          onFocus={(droneId) => {
            setCamera(null)
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
