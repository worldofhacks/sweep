import { useEffect, useReducer, useState } from 'react'
import './control.css'
import { formatElapsed } from '../../shell/format'
import { MISSION_PASS_RULE, MISSION_PASS_TEXT, MISSION_STEPS } from './controls'

const TICK_MS = 1_000

export interface MissionTrackerProps {
  now: () => number
}

/**
 * Appendix E, the scripted mission: ten rows, each naming the gesture, the
 * canonical intent it emits and what the relay does with it at this
 * milestone. Pressing a row marks it done; the clock runs from the first press.
 */
export function MissionTracker({ now }: MissionTrackerProps) {
  const [step, setStep] = useState(0)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const running = startedAt !== null && step < MISSION_STEPS.length
  useTicker(running)
  const elapsed = startedAt === null ? '0:00' : formatElapsed(now() - startedAt)

  return (
    <div>
      <div className="ct-mission-head">
        <p className="ct-mission-intro">
          Appendix E of the PRD. Each step names the gesture, the canonical intent it emits, and what the relay
          does with it at this milestone. Gestures need a classifier score of 0.8 and 600 ms of dwell — 400 ms
          for confirm and cancel.
        </p>
        <div className="ct-mission-clock">
          <p className="ct-mission-elapsed" aria-label="Elapsed">
            {elapsed}
          </p>
          <p className="ct-mission-elapsed-label">elapsed of 3:00</p>
        </div>
      </div>
      <ol className="ct-mission-list">
        {MISSION_STEPS.map((mission, index) => {
          const done = index < step
          const current = index === step
          return (
            <li key={mission.n}>
              <button
                type="button"
                className="ct-mission-row"
                aria-current={current ? 'step' : undefined}
                onClick={() => {
                  setStep((value) => Math.min(MISSION_STEPS.length, value + 1))
                  setStartedAt((value) => value ?? now())
                }}
              >
                <span
                  className={done ? 'ct-mission-mark is-done' : current ? 'ct-mission-mark is-current' : 'ct-mission-mark'}
                  aria-hidden="true"
                >
                  {done ? '✓' : mission.n}
                </span>
                <span className="ct-mission-body">
                  <span className="ct-mission-gesture">{mission.gesture}</span>{' '}
                  <span className="ct-mission-note">{mission.note}</span>
                </span>{' '}
                <span className="ct-mission-intent">{mission.intent}</span>{' '}
                <span className={`ct-mission-status tone-${mission.status === 'unsupported' ? 'warn' : 'ok'}`}>
                  {mission.status}
                </span>
              </button>
            </li>
          )
        })}
      </ol>
      <p className="ct-mission-pass">{step >= MISSION_STEPS.length ? MISSION_PASS_TEXT : MISSION_PASS_RULE}</p>
      <button
        type="button"
        className="ct-mission-reset"
        onClick={() => {
          setStep(0)
          setStartedAt(null)
        }}
      >
        Reset the run
      </button>
    </div>
  )
}

/** Re-renders once a second only while the mission clock is running. */
function useTicker(active: boolean): void {
  const [, tick] = useReducer((count: number) => count + 1, 0)
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => tick(), TICK_MS)
    return () => clearInterval(id)
  }, [active])
}
