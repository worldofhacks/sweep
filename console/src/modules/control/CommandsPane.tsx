import { clampTranslateSteps, createTranslateArgs } from '../../control/intent'
import type { ModuleProps } from '../types'
import { TranslatePad } from './SwarmPane'
import {
  altitudeControls,
  commandCatalog,
  dpadBlockedReason,
  formationControls,
  type CatalogRow,
  type ControlSpec,
} from './controls'

export interface CommandsPaneProps {
  controller: ModuleProps['controller']
  steps: number
  onSteps: (steps: number) => void
}

/** Control › Commands: the catalogue, then the translate pad, formations and altitude. */
export function CommandsPane({ controller, steps, onSteps }: CommandsPaneProps) {
  const { state, issueIntent, selectAllReady } = controller
  const dpadReason = dpadBlockedReason(state)
  const run = (spec: ControlSpec) => {
    if (spec.name === 'select') selectAllReady()
    else issueIntent(spec.press)
  }
  const statusTone = (row: CatalogRow) =>
    row.status === 'accepted at M2.0' ? 'ok' : row.status === 'unsupported' ? 'warn' : 'muted'

  return (
    <div>
      {commandCatalog(state).map((group) => (
        <div key={group.title} className="ct-catalog-group" role="group" aria-label={`${group.title} commands`}>
          <p className="ct-eyebrow">{group.title}</p>
          {group.rows.map((row) => (
            <button
              key={row.key}
              type="button"
              className="ct-catalog-row"
              disabled={!row.enabled}
              title={row.note}
              onClick={() => row.spec && run(row.spec)}
            >
              <span className="ct-catalog-main">
                <span className="ct-catalog-label">{row.label}</span>{' '}
                <span className={`ct-catalog-note tone-${row.noteTone}`}>{row.note}</span>
              </span>{' '}
              <span className="ct-catalog-intent">{row.intent}</span>{' '}
              <span className="ct-catalog-confirm">{row.confirm}</span>{' '}
              <span className="ct-catalog-rule">{row.rule}</span>{' '}
              <span className={`ct-catalog-status tone-${statusTone(row)}`}>{row.status}</span>
            </button>
          ))}
        </div>
      ))}
      <div className="ct-commands-extras">
        <div>
          <p className="ct-eyebrow">Translate</p>
          <TranslatePad
            commands
            blockedReason={dpadReason}
            onTranslate={(direction) =>
              issueIntent({ name: 'translate', args: createTranslateArgs(direction, steps) })
            }
          />
          {dpadReason && <p className="ct-dpad-note">{dpadReason}</p>}
        </div>
        <label className="ct-steps is-commands">
          Steps per press
          <input
            type="number"
            min={1}
            max={6}
            value={steps}
            onChange={(event) => onSteps(clampTranslateSteps(Number(event.target.value)))}
          />
        </label>
        <div>
          <p className="ct-eyebrow">Formation — unsupported at M2.0</p>
          <div className="ct-static-chips" role="group" aria-label="Formation set">
            {formationControls(state).map((spec) => (
              <button
                key={spec.key}
                type="button"
                className="ct-static-chip"
                disabled={!spec.enabled}
                title={spec.note}
                onClick={() => run(spec)}
              >
                {spec.label}
              </button>
            ))}
          </div>
          <p className="ct-commands-note">
            Relay reports {state.formation ?? 'no formation'} at{' '}
            {state.spacing === null ? 'unreported spacing' : `${state.spacing} m`}.
          </p>
        </div>
        <div>
          <p className="ct-eyebrow">Altitude — unsupported at M2.0</p>
          <div className="ct-static-chips" role="group" aria-label="Altitude">
            {altitudeControls(state).map((spec) => (
              <button
                key={spec.key}
                type="button"
                className="ct-static-chip is-sans"
                disabled={!spec.enabled}
                title={spec.note}
                onClick={() => run(spec)}
              >
                {spec.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
