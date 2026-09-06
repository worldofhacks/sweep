import { formatDroneId } from '../../control/state'
import { flightActionBlockedReason, flightActionLabel, flightIntentRequest } from '../../gesture/flight'
import { FLIGHT_GESTURE_PAIRS, type FlightDraftAction } from '../../gesture/policy'
import type { ModuleProps } from '../types'

const poses: Record<string, string> = {
  Open_Palm: 'Open palm',
  Pointing_Up: 'Pointing up',
  Victory: 'Victory / two fingers',
  Closed_Fist: 'Closed fist',
  ILoveYou: 'I love you / thumb, index and little finger',
}

/** Buttons use the console connection, so camera permission is never a prerequisite. */
export function FlightControls({ controller }: Pick<ModuleProps, 'controller'>) {
  const targets = controller.state.selection.map(formatDroneId).join(', ') || 'none selected'
  return (
    <section className="gs-flight" aria-label="Selected flight actions">
      <p className="gs-eyebrow is-first">Flight · {targets}</p>
      <p className="gs-intro">
        Arm session enables commands; it does not start motors. Forward and backward use each aircraft’s
        body frame at 250 mm/s for 500 ms (nominal 0.125 m). These are timed pulses, not distance moves.
      </p>
      <div className="gs-flight-actions">
        {FLIGHT_GESTURE_PAIRS.filter((pair) => pair.action.kind === 'draft').map((pair) => {
          const action = pair.action as FlightDraftAction
          const blocked = flightActionBlockedReason(controller.state, controller.pendingRequest, action)
          const reasonId = `flight-reason-${pair.gesture}`
          return (
            <div key={pair.gesture} className="gs-flight-action">
              <button
                type="button"
                className={blocked ? 'tg-quick is-blocked' : 'tg-quick'}
                disabled={blocked !== null}
                aria-describedby={reasonId}
                onClick={() => controller.prepareIntent(flightIntentRequest(action, controller.state.selection), 'console')}
              >
                {flightActionLabel(action)}
              </button>
              <span>{poses[pair.gesture]}</span>
              <p id={reasonId}>{blocked ?? 'Draft a preview; confirm in the dock to send.'}</p>
            </div>
          )
        })}
      </div>
      <p className="gs-safety-note">
        Every action needs a separate confirmation. Gesture drafts use thumb up to confirm or thumb down to
        cancel, after releasing to neutral. Button drafts use the confirmation dock. HOLD and Land all remain above.
      </p>
    </section>
  )
}
