import type { ControlState } from '../../control/state'
import { formatDroneId } from '../../control/state'
import type { CapturePattern } from '../../relay/contract'
import { formatSelection } from '../../shell/format'
import { PanelHeading } from '../shared'
import type { ModuleProps } from '../types'

/** Room capture controls and Hold, moved from the checkpoint dashboard unchanged. */
export function CapturePanel({
  controller,
  roomId,
  onRoomIdChange,
}: {
  controller: ModuleProps['controller']
  roomId: string
  onRoomIdChange: (roomId: string) => void
}) {
  const { state, prepareCapture, changeCapturePattern, issueHold } = controller
  const captureBlockedReason = getCaptureBlockedReason(state, roomId)
  return (
    <section className="panel control-panel" aria-labelledby="capture-title">
      <PanelHeading
        eyebrow="Outcome request"
        title="Room capture"
        meta="Confirmation required"
        id="capture-title"
      />
      <label className="field-label" htmlFor="room-id">Room identifier</label>
      <input
        className="text-field mono"
        id="room-id"
        value={roomId}
        onChange={(event) => onRoomIdChange(event.target.value)}
        autoComplete="off"
        spellCheck={false}
      />

      <fieldset className="pattern-fieldset">
        <legend>Capture pattern</legend>
        <div className="pattern-options" role="radiogroup" aria-label="Capture pattern">
          <PatternButton
            pattern="pano_360"
            active={state.capturePattern === 'pano_360'}
            onSelect={changeCapturePattern}
            title="Pano 360"
            detail="Full equirectangular artifact required"
          />
          <PatternButton
            pattern="reconstruct_8"
            active={state.capturePattern === 'reconstruct_8'}
            onSelect={changeCapturePattern}
            title="Reconstruct 8"
            detail="Incomplete vertical coverage"
          />
        </div>
      </fieldset>

      <div className={captureBlockedReason ? 'readiness-box is-blocked' : 'readiness-box'}>
        <span className="eyebrow">Capture readiness</span>
        <strong>{captureBlockedReason ? 'Blocked' : 'Ready to preview'}</strong>
        <p>{captureBlockedReason ?? 'One ready aircraft and the requested camera pattern are available.'}</p>
      </div>

      <button
        type="button"
        className="primary-action"
        disabled={Boolean(captureBlockedReason)}
        onClick={() => prepareCapture(roomId)}
      >
        Capture room <span>Build preview</span>
      </button>
      <button
        type="button"
        className="secondary-action"
        disabled={state.selection.length === 0 || state.connection.status !== 'connected'}
        onClick={issueHold}
      >
        Hold selected <span>{formatSelection(state.selection)}</span>
      </button>
    </section>
  )
}

function PatternButton({ pattern, active, onSelect, title, detail }: {
  pattern: CapturePattern
  active: boolean
  onSelect: (pattern: CapturePattern) => void
  title: string
  detail: string
}) {
  return (
    <button
      type="button"
      className={active ? 'pattern-button is-active' : 'pattern-button'}
      role="radio"
      aria-checked={active}
      onClick={() => onSelect(pattern)}
    >
      <strong>{title}</strong>
      <span>{detail}</span>
    </button>
  )
}

function getCaptureBlockedReason(state: ControlState, roomId: string): string | null {
  if (state.connection.status !== 'connected') return 'Relay is not authenticated. No intent can be sent.'
  if (!roomId.trim()) return 'Enter a room identifier.'
  if (state.selection.length !== 1) return 'Select exactly one ready aircraft.'
  const selected = state.aircraft[state.selection[0]]
  if (!selected || selected.membership !== 'ready' || !selected.selectable) {
    return 'The selected aircraft is not ready or selectable.'
  }
  if (!selected.camera_patterns.includes(state.capturePattern)) {
    return `${formatDroneId(selected.drone_id)} does not report ${state.capturePattern}; the console will not substitute a pattern.`
  }
  return null
}
