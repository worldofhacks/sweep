import { clampTranslateSteps, createTranslateArgs } from '../../control/intent'
import {
  capabilityBlockedReason,
  deviceLabeller,
  isIntentEnabled,
  rosterNoun,
  selectionNoun,
} from '../../control/state'
import type { ControlState, RequestRecord } from '../../control/state'
import { planTitle } from '../../control/plan'
import { sortedAircraft } from '../../shell/derive'
import type { ModuleProps } from '../types'
import {
  DPAD_CELLS,
  FORMATION_NAMES,
  MOTION_FOOTNOTE,
  aircraftChips,
  chipBlockers,
  dpadBlockedReason,
  fanoutFor,
  fleetControls,
  formationControls,
  formationPlot,
  formationRelayNote,
  motionControls,
  noReadyReason,
  readyIds,
  supervisedVerticalProfile,
  type ControlSpec,
} from './controls'

export interface SwarmPaneProps {
  controller: ModuleProps['controller']
  steps: number
  onSteps: (steps: number) => void
  formationPreview: string | null
  onFormationPreview: (name: string) => void
}

/** Control › Swarm: target, chips, fleet and motion controls, translate pad, formation panel. */
export function SwarmPane({ controller, steps, onSteps, formationPreview, onFormationPreview }: SwarmPaneProps) {
  const { state, pendingRequest, issueIntent, toggleAircraft, selectAllReady } = controller
  const chips = aircraftChips(state)
  const blockers = chipBlockers(state)
  const ready = readyIds(state)
  const selectEnabled = isIntentEnabled(state, 'select')
  const dpadReason = dpadBlockedReason(state)
  const rosterWord = rosterNoun(sortedAircraft(state.aircraft))
  const supervisedVertical = supervisedVerticalProfile(state)
  const fleet = fleetControls(state).filter((spec) => !supervisedVertical || spec.supported)
  const motion = motionControls(state).filter((spec) => !supervisedVertical || spec.supported)
  const run = (spec: ControlSpec) => {
    if (spec.name === 'select') selectAllReady()
    else issueIntent(spec.press)
  }

  return (
    <div data-two="1" className="ct-two">
      <div className="ct-column">
        <div className="ct-target-row">
          <p className="ct-target">
            <span className="ct-eyebrow">Target</span>
            <span className="ct-target-count">
              {state.selection.length} of {chips.length} selected
            </span>
          </p>
          <button
            type="button"
            className="ct-select-all"
            disabled={!selectEnabled || ready.length === 0}
            title={
              capabilityBlockedReason(state, 'select') ??
              (ready.length === 0 ? noReadyReason(state) : undefined)
            }
            onClick={selectAllReady}
          >
            Select all ready
          </button>
        </div>

        <div className="ct-selection-groups" role="group" aria-label="Select device class">
          {(['aircraft', 'ground_vehicle'] as const).map((deviceClass) => {
            const ids = ready.filter((id) => state.aircraft[id].device_class === deviceClass)
            return <button key={deviceClass} type="button" className="ct-select-all"
              disabled={!selectEnabled || ids.length === 0}
              onClick={() => issueIntent({ name: 'select', args: { ids }, targets: ids })}>
              {deviceClass === 'aircraft' ? 'Select aircraft' : 'Select robots'}
            </button>
          })}
        </div>
        <p className="ct-dpad-note">Toggle devices to build a group, or choose Only to move one device.</p>
        <div className="ct-chips" role="group" aria-label="Devices">
          {chips.map((chip) => (
            <span key={chip.droneId} className="ct-chip-group"><button
              key={chip.droneId}
              type="button"
              className={chip.selected ? 'ct-chip is-selected' : 'ct-chip'}
              aria-pressed={chip.selected}
              disabled={!chip.selectable || (chip.selected && state.selection.length === 1)}
              title={chip.reason || undefined}
              onClick={() => toggleAircraft(chip.droneId)}
            >
              <span className="ct-chip-id">{chip.id}</span>{' '}
              <span className="ct-chip-sub">{chip.sub}</span>
            </button><button type="button" className="ct-select-only" disabled={!chip.selectable}
              aria-label={`Select only ${chip.id}`} onClick={() => issueIntent({ name: 'select', args: { ids: [chip.droneId] }, targets: [chip.droneId] })}>Only</button></span>
          ))}
        </div>
        {blockers && (
          <p className="ct-blockers">
            <span className="ct-blockers-mark" aria-hidden="true">
              ▲{' '}
            </span>
            {blockers}
          </p>
        )}

        <p className="ct-eyebrow">Fleet</p>
        <div className="ct-fleet-row" role="group" aria-label="Fleet controls">
          {fleet.map((spec) => (
            <ControlButton key={spec.key} spec={spec} onPress={run} />
          ))}
        </div>

        <div className="ct-motion-wrap">
          <div className="ct-motion">
            <p className="ct-eyebrow">Motion — every selected {rosterWord}</p>
            <div className="ct-motion-list" role="group" aria-label="Motion controls">
              {motion.map((spec) => (
                <ControlButton key={spec.key} spec={spec} motion onPress={run} />
              ))}
            </div>
            <p className="ct-motion-foot">{MOTION_FOOTNOTE}</p>
          </div>
          {!supervisedVertical && (
            <div className="ct-dpad-wrap">
            <p className="ct-eyebrow">Translate together</p>
            <TranslatePad
              blockedReason={dpadReason}
              onTranslate={(direction) =>
                issueIntent({ name: 'translate', args: createTranslateArgs(direction, steps) })
              }
            />
            <label className="ct-steps">
              Steps
              <input
                type="number"
                min={1}
                max={6}
                value={steps}
                aria-label="Steps per press"
                onChange={(event) => onSteps(clampTranslateSteps(Number(event.target.value)))}
              />
            </label>
            <p className="ct-dpad-note">Robots: room east +x, north +y. Aircraft: relay-configured translation frame. One step is configured by the relay.</p>
            {dpadReason && <p className="ct-dpad-note">{dpadReason}</p>}
            </div>
          )}
        </div>
      </div>

      <div className="ct-column">
        {!supervisedVertical && (
          <FormationPanel
            controller={controller}
            preview={formationPreview}
            onPreview={onFormationPreview}
          />
        )}
        {pendingRequest && <FanoutCard pending={pendingRequest} state={state} />}
      </div>
    </div>
  )
}

export function ControlButton({
  spec,
  motion = false,
  onPress,
}: {
  spec: ControlSpec
  motion?: boolean
  onPress: (spec: ControlSpec) => void
}) {
  const classes = ['ct-control-btn']
  if (motion) classes.push('is-motion')
  if (!spec.supported) classes.push('is-unsupported')
  return (
    <div className="ct-control-item">
      <button
        type="button"
        className={classes.join(' ')}
        disabled={!spec.enabled}
        title={spec.note}
        onClick={() => onPress(spec)}
      >
        <span>{spec.label}</span>
        {spec.badge && (
          <>
            {' '}
            <span className="ct-badge">{spec.badge}</span>
          </>
        )}
      </button>
      <span className={`ct-note tone-${spec.noteTone}`}>{spec.note}</span>
    </div>
  )
}

export function TranslatePad({
  blockedReason,
  commands = false,
  onTranslate,
}: {
  blockedReason: string | null
  commands?: boolean
  onTranslate: (direction: 'north' | 'south' | 'east' | 'west') => void
}) {
  return (
    <div
      role="group"
      aria-label="Translate direction"
      className={commands ? 'ct-dpad is-commands' : 'ct-dpad'}
    >
      {DPAD_CELLS.map((cell) =>
        cell.direction ? (
          <button
            key={cell.key}
            type="button"
            className="ct-dpad-key"
            aria-label={cell.aria}
            disabled={blockedReason !== null}
            title={blockedReason ?? undefined}
            onClick={() => cell.direction && onTranslate(cell.direction)}
          >
            {cell.label}
          </button>
        ) : (
          <button key={cell.key} type="button" className="ct-dpad-key is-spacer" aria-hidden="true" disabled tabIndex={-1}>
            {cell.label}
          </button>
        ),
      )}
    </div>
  )
}

function FormationPanel({
  controller,
  preview,
  onPreview,
}: {
  controller: ModuleProps['controller']
  preview: string | null
  onPreview: (name: string) => void
}) {
  const { state, issueIntent } = controller
  const shown = preview ?? state.formation
  const selected = sortedAircraft(state.aircraft).filter((drone) => state.selection.includes(drone.drone_id))
  const classes = [...new Set(selected.map((device) => device.device_class))]
  const groups = classes.map((deviceClass) => {
    const count = selected.filter((device) => device.device_class === deviceClass).length
    return { deviceClass, count, dots: formationPlot(count, shown, state.spacing) }
  })
  const dots = groups.flatMap((group) => group.dots.map((dot) => ({ ...dot,
    id: classes.length > 1 ? `${group.deviceClass === 'aircraft' ? 'Aircraft' : 'Robot'} ${dot.id}` : dot.id,
  })))
  const options = formationControls(state)
  return (
    <div className="ct-panel" aria-label="Formation">
      <p className="ct-formation-head">
        <span className="ct-eyebrow">Formation</span>
        <span className="ct-formation-name">{shown ?? 'unreported'}</span>
        <span className="ct-formation-spacing">
          spacing {state.spacing === null ? 'unreported' : `${state.spacing.toFixed(1)} m`}
        </span>
      </p>
      <div className="ct-formation-options" role="group" aria-label="Formation options">
        {FORMATION_NAMES.map((name, index) => {
          const spec = options[index]
          return (
            <button
              key={name}
              type="button"
              className={shown === name ? 'ct-formation-option is-active' : 'ct-formation-option'}
              aria-pressed={shown === name}
              disabled={!spec.enabled}
              title={spec.note}
              onClick={() => {
                onPreview(name)
                issueIntent(spec.press)
              }}
            >
              {name}
            </button>
          )
        })}
      </div>
      {groups.map((group) => <section key={group.deviceClass} aria-label={`${group.deviceClass === 'aircraft' ? 'Aircraft' : 'Robot'} formation preview`}>
        {classes.length > 1 && <p className="ct-eyebrow">{group.deviceClass === 'aircraft' ? 'Aircraft' : 'Robots'} · {group.count}</p>}
        {classes.length > 1 && group.count === 1 ? <p className="ct-dpad-note">Single device holds its current pose.</p> : <div className="ct-plot" aria-hidden="true">
          {group.dots.map((dot) => <span key={dot.id} className="ct-plot-dot" style={{ left: dot.left, top: dot.top }}>{dot.id}</span>)}
        </div>}
      </section>)}
      <p className="ct-formation-relay">{formationRelayNote(preview, state.formation)}</p>
      <p className="ct-formation-planner">
        Shape-only slots: device-to-slot assignments are not projected by the relay and are therefore not
        guessed here. Robots form on the floor plane; aircraft retain their flight altitude. Mixed groups
        form independently within each class, with spacing checked within that class. The arbiter refuses the whole plan if any assigned route breaks spacing, the ceiling or
        the geofence. The requested shape is not authoritative until relay state reports the completed update.
      </p>
      {dots.map((dot) => (
        <p key={dot.id} className="ct-slot-row">
          <strong>{dot.id}</strong>
          <span>{dot.slot}</span>
        </p>
      ))}
    </div>
  )
}

function FanoutCard({ pending, state }: { pending: RequestRecord; state: ControlState }) {
  const rows = fanoutFor(
    pending.intent.name,
    pending.intent.args,
    pending.intent.selection,
    deviceLabeller(state.aircraft),
  )
  const noun = selectionNoun(state.aircraft, pending.intent.selection)
  return (
    <div className="ct-plan-card" aria-label={`Per-${noun} fan-out`}>
      <p className="ct-eyebrow">Per-{noun} fan-out</p>
      <h2 className="ct-plan-title">{pending.plan?.title ?? planTitle(pending.intent)}</h2>
      <p className="ct-fanout-note">
        {rows.length === 0
          ? 'The draft names no device; the planner fans out over the roster it holds.'
          : `The planner proposes ${rows.length} per-${noun} commands. The arbiter checks each one before dispatch.`}
      </p>
      {rows.map((row) => (
        <p key={row.id} className="ct-fanout-row">
          <strong>{row.id}</strong>
          <span>{row.cmd}</span>
        </p>
      ))}
    </div>
  )
}
