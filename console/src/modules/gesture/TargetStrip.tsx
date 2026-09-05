import { formatDroneId, type ControlState, type RequestRecord } from '../../control/state'
import { isSupportedIntent, selectionRule } from '../../relay/contract'
import { isReady, sortedAircraft } from '../../shell/derive'
import { formatPercent, humanizeCode } from '../../shell/format'
import { rosterIds } from '../control/controls'
import type { ModuleProps } from '../types'

type Controller = ModuleProps['controller']

interface QuickCommandSpec {
  name: 'takeoff' | 'hold' | 'come_home' | 'land_all'
  label: string
  /** The dock must confirm before anything is sent. */
  confirm: boolean
  /** Design copy appended to the note; one full sentence. */
  detail?: string
  /** What a press does through the control hook: draft a preview, or send at once. */
  run: (controller: Controller) => void
}

/**
 * The design's quick commands, in its order. Hold drafts a forced preview;
 * takeoff and land_all are confirmation-gated by the contract, so issueIntent
 * parks them in the dock; come_home is not, so issueIntent sends it at once.
 */
const QUICK_COMMANDS: readonly QuickCommandSpec[] = [
  {
    name: 'takeoff',
    label: 'Takeoff',
    confirm: true,
    run: (controller) => controller.issueIntent({ name: 'takeoff', args: {} }),
  },
  { name: 'hold', label: 'Hold', confirm: true, run: (controller) => controller.prepareHold('console') },
  {
    name: 'come_home',
    label: 'Come home',
    confirm: false,
    run: (controller) => controller.issueIntent({ name: 'come_home', args: {} }),
  },
  {
    name: 'land_all',
    label: 'Land all',
    confirm: true,
    detail: 'It targets every aircraft in the roster.',
    run: (controller) =>
      controller.issueIntent({ name: 'land_all', args: {}, targets: rosterIds(controller.state) }),
  },
]

interface QuickCommandView {
  badge: 'confirm' | 'unsupported' | null
  /** Why the button is disabled; null when a press drafts a preview or sends. */
  reason: string | null
  /** The title: the reason, or what a press does. */
  note: string
}

/**
 * Mirrors the design's control gating order: unsupported name, console
 * connection, network stop, pending preview, empty selection, readiness.
 * land_all addresses the roster, so it needs a roster rather than a selection.
 */
function quickCommandView(
  spec: QuickCommandSpec,
  state: ControlState,
  pending: RequestRecord | null,
): QuickCommandView {
  if (!isSupportedIntent(spec.name)) {
    const sentences = [
      `The relay refuses ${spec.name} as unsupported; it is listed until the relay accepts it.`,
    ]
    if (spec.confirm) sentences.push('Confirmation would be required before send.')
    if (spec.detail) sentences.push(spec.detail)
    return { badge: 'unsupported', reason: sentences.join(' '), note: sentences.join(' ') }
  }
  const badge = spec.confirm ? 'confirm' : null
  const wholeRoster = selectionRule(spec.name) === 'all'
  const targets = wholeRoster ? rosterIds(state) : state.selection
  let reason: string | null = null
  if (state.connection.status !== 'connected') {
    reason = `The console connection is ${state.connection.status}. Nothing can be sent.`
  } else if (state.estop) {
    reason = 'The network stop is active. Motion intents are refused until the relay reports it clear.'
  } else if (pending) {
    reason = 'Confirm or cancel the pending preview first.'
  } else if (targets.length === 0) {
    reason = wholeRoster ? 'No aircraft in the roster.' : 'No aircraft selected.'
  } else if (!wholeRoster) {
    const notReady = targets.filter((id) => !isReady(state.aircraft[id]))
    if (notReady.length > 0) {
      reason = `${notReady.map(formatDroneId).join(', ')} ${notReady.length > 1 ? 'are' : 'is'} not ready.`
    }
  }
  const named = targets.map(formatDroneId).join(', ')
  const action = spec.confirm
    ? `Drafts a ${spec.name} preview for ${named}; nothing is sent until the dock confirms it.`
    : `Sends ${spec.name} to ${named} at once; the relay's answer is recorded under Requests.`
  const note = reason ?? (spec.detail ? `${action} ${spec.detail}` : action)
  return { badge, reason, note }
}

/**
 * The target strip the design shows above the Gesture and Speech panes: who is
 * selected, chips that toggle selection through the relay, the aircraft that
 * cannot be commanded, and the quick commands. "All ready", Hold, Takeoff and
 * Land all draft a preview for the dock and Come home sends at once, all
 * through the control hook; a name outside the relay's capability set is listed as
 * unsupported rather than sent.
 */
export function TargetStrip({ controller }: { controller: Controller }) {
  const { state, pendingRequest, toggleAircraft, prepareSelect } = controller
  const fleet = sortedAircraft(state.aircraft)
  const ready = fleet.filter(isReady).map((drone) => drone.drone_id)
  const blockers = fleet.filter((drone) => !isReady(drone))
  const allReadySelected = ready.length > 0 && ready.every((id) => state.selection.includes(id))
  const allReadyDisabled = ready.length === 0 || allReadySelected || pendingRequest !== null
  return (
    <div className="tg-strip" role="group" aria-label="Target">
      <span className="tg-strip-count">
        <span className="tg-eyebrow">Target</span>
        <span className="tg-strip-selected">
          {state.selection.length} of {fleet.length} selected
        </span>
      </span>
      <span className="tg-strip-chips">
        {fleet.map((drone) => {
          const can = isReady(drone)
          const on = state.selection.includes(drone.drone_id)
          const lastSelected = on && state.selection.length === 1
          const reason = can
            ? lastSelected
              ? 'Intent v1 requires at least one aircraft in a select request.'
              : undefined
            : humanizeCode(drone.readiness_reasons[0] ?? drone.membership)
          const classes = ['tg-chip']
          if (on) classes.push('is-selected')
          if (!can) classes.push('is-blocked')
          return (
            <button
              key={drone.drone_id}
              type="button"
              className={classes.join(' ')}
              aria-pressed={on}
              aria-label={`${on ? 'Deselect' : 'Select'} ${formatDroneId(drone.drone_id)}`}
              disabled={!can || lastSelected}
              title={reason}
              onClick={() => toggleAircraft(drone.drone_id)}
            >
              <span className="tg-chip-id">{formatDroneId(drone.drone_id)}</span>
              <span className="tg-chip-sub">
                {drone.flight_state ?? 'unreported'} · {formatPercent(drone.battery)}
              </span>
            </button>
          )
        })}
        <button
          type="button"
          className="tg-strip-all"
          disabled={allReadyDisabled}
          title={
            pendingRequest
              ? 'Confirm or cancel the pending preview first.'
              : allReadySelected
                ? 'Every ready aircraft is already selected.'
                : ready.length === 0
                  ? 'No aircraft is ready.'
                  : 'Drafts a select preview for every ready aircraft.'
          }
          onClick={() => prepareSelect(ready, 'console')}
        >
          All ready
        </button>
      </span>
      {blockers.length > 0 && (
        <span className="tg-strip-blockers">
          <span aria-hidden="true" className="tone-danger">
            ▲{' '}
          </span>
          {blockers
            .map(
              (drone) =>
                `${formatDroneId(drone.drone_id)} ${humanizeCode(drone.readiness_reasons[0] ?? 'not selectable').toLowerCase()}`,
            )
            .join(' · ')}{' '}
          — these cannot be selected or commanded.
        </span>
      )}
      <span className="tg-strip-quick" role="group" aria-label="Quick commands">
        {QUICK_COMMANDS.map((spec) => {
          const view = quickCommandView(spec, state, pendingRequest)
          return (
            <button
              key={spec.name}
              type="button"
              className={view.reason ? 'tg-quick is-blocked' : 'tg-quick'}
              aria-label={spec.label}
              disabled={view.reason !== null}
              title={view.note}
              onClick={() => spec.run(controller)}
            >
              <span>{spec.label}</span>
              {view.badge && <span className="tg-quick-badge">{view.badge}</span>}
            </button>
          )
        })}
      </span>
    </div>
  )
}
