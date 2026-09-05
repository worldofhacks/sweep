import { useState } from 'react'
import { isValidRoomId } from '../../control/intent'
import type { RequestRecord } from '../../control/state'
import { formatDroneId } from '../../control/state'
import { planTitle } from '../../control/plan'
import { shortId } from '../../shell/format'
import type { ModuleProps } from '../types'
import {
  PATTERN_CARDS,
  ROOM_RULE,
  aircraftChips,
  captureFlow,
  captureGate,
  compassSectors,
  gateRows,
  guidanceNote,
  sectorSummary,
  type CaptureReadiness,
} from './controls'

export interface CapturePaneProps {
  controller: ModuleProps['controller']
  roomId: string
  onRoomId: (roomId: string) => void
  /** capture_readiness guidance; null until a relay event carries it. */
  guidance: CaptureReadiness | null
}

/** Control › Capture: the three-step flow, room and pattern, Capture room, the guidance mirror, the plan detail. */
export function CapturePane({ controller, roomId, onRoomId, guidance }: CapturePaneProps) {
  const { state, pendingRequest, selectAircraft, changeCapturePattern, prepareCapture, invalidatePending } =
    controller
  const roomOk = isValidRoomId(roomId.trim())
  const flow = captureFlow(state, roomId.trim(), roomOk, guidance)
  const gate = captureGate(state, roomId.trim(), roomOk, guidance)
  const chips = aircraftChips(state)

  const changeRoom = (value: string) => {
    onRoomId(value)
    if (
      pendingRequest &&
      pendingRequest.intent.name === 'capture_room' &&
      'room_id' in pendingRequest.intent.args &&
      pendingRequest.intent.args.room_id !== value.trim()
    ) {
      invalidatePending(
        'configuration_changed',
        'The room identifier changed after this plan was built. Build and confirm a new preview.',
      )
    }
  }

  return (
    <div data-two="1" className="ct-two">
      <div className="ct-column">
        <ol className="ct-flow-list" style={{ margin: 0, padding: 0, listStyle: 'none' }}>
          {flow.map((step) => {
            const on = step.done || step.current
            return (
              <li key={step.n} className="ct-flow">
                <span className={on ? 'ct-flow-mark is-on' : 'ct-flow-mark'} aria-hidden="true">
                  {step.done ? '✓' : step.n}
                </span>
                <div className="ct-flow-body">
                  <h2 className={on ? 'ct-flow-title is-on' : 'ct-flow-title'}>{step.title}</h2>
                  <p className={`ct-flow-state tone-${step.tone}`}>{step.state}</p>
                  <p className="ct-flow-hint">{step.hint}</p>
                </div>
              </li>
            )
          })}
        </ol>

        <div className="ct-capture-chips" role="group" aria-label="Capture aircraft">
          {chips.map((chip) => (
            <div key={chip.droneId} className="ct-capture-chip">
              <button
                type="button"
                className={chip.selected ? 'ct-chip is-capture is-selected' : 'ct-chip is-capture'}
                aria-pressed={chip.selected}
                disabled={!chip.selectable}
                onClick={() => selectAircraft(chip.droneId)}
              >
                <span className="ct-chip-id">{chip.id}</span>{' '}
                <span className="ct-chip-sub">{chip.sub}</span>
              </button>
              {chip.reason && <span className="ct-chip-reason">{chip.reason}</span>}
            </div>
          ))}
        </div>

        <label className="ct-room-label">
          Room identifier
          <input
            className="ct-room-input"
            value={roomId}
            placeholder="kitchen-01"
            aria-invalid={!roomOk}
            aria-describedby="ct-room-msg"
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => changeRoom(event.target.value)}
          />
        </label>
        <p id="ct-room-msg" className={roomOk ? 'ct-room-msg' : 'ct-room-msg is-invalid'}>
          {roomOk ? 'Valid. The capture id is minted from the intent id at draft time.' : ROOM_RULE}
        </p>

        <div className="ct-patterns" role="group" aria-label="Capture pattern">
          {PATTERN_CARDS.map((card) => {
            const active = state.capturePattern === card.id
            return (
              <button
                key={card.id}
                type="button"
                className={active ? 'ct-pattern is-active' : 'ct-pattern'}
                aria-pressed={active}
                onClick={() => changeCapturePattern(card.id)}
              >
                <span className="ct-pattern-id">{card.id}</span>
                <span className="ct-pattern-coverage">{card.coverage}</span>
                <span className="ct-pattern-note">{card.note}</span>
              </button>
            )
          })}
        </div>

        <div className="ct-panel ct-capture-block">
          <p className="ct-capture-sentence">{gate.text}</p>
          <button
            type="button"
            className="ct-capture-btn"
            disabled={!gate.ready}
            onClick={() => prepareCapture(roomId)}
          >
            Capture room
          </button>
          <p className="ct-capture-hint">
            {gate.ready
              ? 'One request. You will see the envelope before it is sent.'
              : 'Finish the steps above to enable this.'}
          </p>
        </div>
      </div>

      <div className="ct-column">
        <GuidancePanel guidance={guidance} />
        {pendingRequest && (
          <PlanDetail key={pendingRequest.intent.intent_id} pending={pendingRequest} />
        )}
      </div>
    </div>
  )
}

/** The capture-readiness mirror: guidance mode, pose source, compass, gates, heading, suggested move. */
export function GuidancePanel({ guidance }: { guidance: CaptureReadiness | null }) {
  const gates = gateRows(guidance)
  const sectors = compassSectors(guidance)
  return (
    <div className="ct-panel" aria-label="Capture readiness">
      <p className="ct-guidance-head">
        <span>guidance {guidance?.guidance_mode ?? 'unreported'}</span>{' '}
        <span>pose {guidance?.pose_source ?? 'unreported'}</span>
      </p>
      <div className="ct-guidance-body">
        <div className="ct-compass" aria-hidden="true">
          {sectors.map((sector) => (
            <div
              key={sector.index}
              className={`ct-sector is-${sector.coverage}`}
              style={{ transform: sector.rotation }}
            />
          ))}
          <div className="ct-compass-centre">
            <span className="ct-heading">
              {guidance?.next_heading_deg === null || guidance === null ? '—' : `${guidance.next_heading_deg}°`}
            </span>
            <span className="ct-heading-label">next heading</span>
          </div>
        </div>
        <ul className="ct-gates" aria-label="Readiness gates">
          {gates.map((gate) => (
            <li key={gate.key} className="ct-gate">
              <span className="ct-gate-key">{gate.key}</span>
              <span className={`ct-gate-word tone-${gate.tone}`}>{gate.word}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="ct-sector-summary">{sectorSummary(guidance)}</p>
      <p className="ct-suggested">
        Suggested move <span>{guidance?.suggested_delta ?? 'unreported'}</span>
      </p>
      <p className="ct-guidance-note">{guidanceNote(guidance)}</p>
    </div>
  )
}

/** The plan detail card: title, targets, roster, steps, and the exact Intent v1 draft, open on every draft. */
function PlanDetail({ pending }: { pending: RequestRecord }) {
  const [jsonOpen, setJsonOpen] = useState(true)
  const intent = pending.intent
  return (
    <div className="ct-plan-card is-detail" role="region" aria-label="Plan detail" tabIndex={-1}>
      <p className="ct-eyebrow">Plan detail</p>
      <h2 className="ct-plan-title">{pending.plan?.title ?? planTitle(intent)}</h2>
      <p className="ct-plan-meta">
        <span className="ct-plan-targets">
          {intent.selection.map(formatDroneId).join('  ') || 'whole roster'}
        </span>{' '}
        <span>source {intent.source}</span>{' '}
        <span>roster v{pending.plan?.rosterVersion ?? 'unreported'}</span>{' '}
        <span title={intent.intent_id}>{shortId(intent.intent_id)}</span>
      </p>
      {pending.plan && pending.plan.steps.length > 0 && (
        <ol className="ct-plan-steps">
          {pending.plan.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      )}
      <button
        type="button"
        className="ct-json-toggle"
        aria-expanded={jsonOpen}
        onClick={() => setJsonOpen((open) => !open)}
      >
        {jsonOpen ? 'Hide Intent v1 envelope' : 'Show Intent v1 envelope'}
      </button>
      {jsonOpen && (
        <div className="ct-json">
          <p className="ct-json-note">
            Exact Intent v1 draft. Confirming stamps t and sets confirm true; nothing else changes.
          </p>
          <pre className="ct-json-pre" data-scroll="1">
            {JSON.stringify(intent, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
