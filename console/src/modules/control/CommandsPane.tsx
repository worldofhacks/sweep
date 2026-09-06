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
    row.status === 'available' ? 'ok' : row.status === 'unsupported' ? 'warn' : 'muted'

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
      {state.enabledIntentNames.includes('navigate') && state.navigation !== null && (
        <div className="ct-navigation" aria-label="Navigation destination">
          <p className="ct-eyebrow">Navigate</p>
          <p className="ct-commands-note">Routes use the advertised map and end in an arrival hold.</p>
          <div className="ct-static-chips" role="group" aria-label="Destination zones">
            {state.navigation.zones.filter((zone) => zone.navigation_allowed && zone.floor_id === state.navigation?.floor_id).map((zone) => (
              <button key={zone.zone_id} type="button" className="ct-static-chip is-sans"
                disabled={state.selection.length === 0}
                onClick={() => issueIntent({ name: 'navigate', args: { zone_id: zone.zone_id } })}>
                {zone.aliases[0] ?? zone.zone_id}
              </button>
            ))}
          </div>
        </div>
      )}
      {state.navigation?.formations?.length && state.enabledIntentNames.includes('formation_set') ? (
        <div className="ct-navigation" aria-label="Mapped formations">
          <p className="ct-eyebrow">Mapped formations</p>
          <div className="ct-static-chips" role="group" aria-label="Mapped formation set">
            {state.navigation.formations.map((formation) => (
              <button key={formation.name} type="button" className="ct-static-chip is-sans" disabled={!state.armed || state.selection.length === 0}
                onClick={() => issueIntent({ name: 'formation_set', args: { name: formation.name as never } })}>
                {formation.name}
              </button>
            ))}
          </div>
        </div>
      ) : null}
      {state.navigation?.search && state.enabledIntentNames.includes('search') ? (
        <div className="ct-navigation" aria-label="Search mission">
          <p className="ct-eyebrow">Search mission</p>
          <div className="ct-static-chips" role="group" aria-label="Search targets">
            {state.navigation.search.zones.flatMap((zone) => state.navigation!.search!.target_classes.map((targetClass) => (
              <button key={`${zone.zone_id}-${targetClass}`} type="button" className="ct-static-chip is-sans" disabled={!state.armed || state.selection.length === 0}
                onClick={() => issueIntent({ name: 'search', args: { zone_id: zone.zone_id, target_class: targetClass } })}>
                {targetClass} · {zone.zone_id}
              </button>
            )))}
          </div>
          {state.searchProgress && <p className="ct-commands-note" aria-live="polite">Search {state.searchProgress.state}: {state.searchProgress.tasks.reduce((covered, task) => covered + task.covered_cells, 0)} / {state.searchProgress.tasks.reduce((total, task) => total + task.total_cells, 0)} cells covered.</p>}
          {state.sightings.length > 0 && <p className="ct-commands-note" aria-live="polite">Latest sighting: {state.sightings[0].label} at {Math.round(state.sightings[0].confidence * 100)}% confidence.</p>}
        </div>
      ) : null}
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
          <p className="ct-eyebrow">Formation</p>
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
          <p className="ct-eyebrow">Altitude</p>
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
