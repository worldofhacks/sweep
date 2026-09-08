import { useState } from 'react'
import { isTerminalRequest, type ControlState, type RequestRecord } from '../../control/state'
import { formatTime, shortId } from '../../shell/format'

const NOTE_LIMIT = 24
const STORAGE_PREFIX = 'sweep.operator-observations.v1:'

interface OperatorObservation {
  source: 'operator'
  binding: string
  observer: string
  note: string
  recordedAt: number
}

/** Binds an annotation to one retained request, never to an inferred physical outcome. */
function requestBinding(request: RequestRecord): string {
  const { intent, plan } = request
  return JSON.stringify({
    session: intent.session,
    intentId: intent.intent_id,
    intentTimestamp: intent.t,
    retryOf: intent.retry_of,
    mode: intent.mode,
    source: intent.source,
    name: intent.name,
    args: Object.fromEntries(Object.entries(intent.args).sort(([a], [b]) => a.localeCompare(b))),
    selection: [...intent.selection].sort((a, b) => a - b),
    epochs: [...intent.selection].sort((a, b) => a - b).map((id) => [id, plan?.deviceEpochs?.[id] ?? null]),
  })
}

function storedObservations(session: string): OperatorObservation[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_PREFIX + session)
    if (!raw || raw.length > 250_000) return []
    const value: unknown = JSON.parse(raw)
    if (!Array.isArray(value) || value.length > NOTE_LIMIT) return []
    return value.filter((entry): entry is OperatorObservation => entry !== null && typeof entry === 'object' &&
      entry.source === 'operator' && typeof entry.binding === 'string' && entry.binding.length <= 8192 &&
      typeof entry.observer === 'string' && entry.observer.trim().length > 0 && entry.observer.length <= 64 &&
      typeof entry.note === 'string' && entry.note.trim().length > 0 && entry.note.length <= 512 &&
      Number.isSafeInteger(entry.recordedAt) && entry.recordedAt >= 0 && entry.recordedAt <= 8_640_000_000_000_000)
  } catch {
    return []
  }
}

/** Session request evidence plus explicitly unverified operator annotations. No command interface. */
export function SessionRunEvidence({ state, now = Date.now }: { state: ControlState; now?: () => number }) {
  return <SessionEvidence key={state.sessionId} state={state} now={now} />
}

function SessionEvidence({ state, now }: { state: ControlState; now: () => number }) {
  const [observations, setObservations] = useState(() => storedObservations(state.sessionId))
  const [selectedBinding, setSelectedBinding] = useState('')
  const [observer, setObserver] = useState('')
  const [note, setNote] = useState('')
  const [storageMessage, setStorageMessage] = useState('')
  const requests = state.requests.filter((request) => request.intent.session === state.sessionId)
  const terminal = requests.filter((request) => isTerminalRequest(request.status))
  const selected = terminal.find((request) => requestBinding(request) === selectedBinding)
  const visibleNotes = observations.filter((entry) => terminal.some((request) => requestBinding(request) === entry.binding))
  const completed = requests.filter((request) => request.status === 'completed').length
  const unsuccessful = requests.filter((request) => request.status === 'failed' || request.status === 'refused').length
  const unsent = requests.filter((request) => ['draft', 'pending_confirmation', 'cancelled', 'invalidated'].includes(request.status)).length
  const waiting = requests.length - completed - unsuccessful - unsent
  const canRecord = selected !== undefined && observer.trim().length > 0 && note.trim().length > 0

  return <section className="ct-mission" aria-label="Session run evidence">
    <div className="ct-mission-head">
      <div>
        <p className="ct-eyebrow">Session run evidence</p>
        <p className="ct-mission-intro">Retained request records for <code>{state.sessionId}</code>. Connection: {state.connection.status}.</p>
      </div>
      <p className="ct-mission-note">Request outcomes do not prove physical movement.</p>
    </div>
    <dl className="ct-evidence-counts">
      <div><dt>Relay-reported completed</dt><dd>{completed}</dd></div>
      <div><dt>Failed or refused</dt><dd>{unsuccessful}</dd></div>
      <div><dt>Drafts, cancellations or invalidations</dt><dd>{unsent}</dd></div>
      <div><dt>Awaiting outcome</dt><dd>{waiting}</dd></div>
    </dl>
    {requests.length === 0 && <p className="ct-mission-note">No request evidence has been received or created in this session.</p>}
    <details className="ct-evidence-observations">
      <summary>Operator physical observations · {visibleNotes.length} recorded</summary>
      <p className="ct-mission-note">Manual notes are unverified operator evidence. They never change request status, qualify a device, or issue a command. Notes stay in this browser tab and are shown only with their matching session request.</p>
      <form className="ct-evidence-form" onSubmit={(event) => {
        event.preventDefault()
        if (!canRecord || !selected) return
        const recordedAt = now()
        if (!Number.isSafeInteger(recordedAt) || recordedAt < 0 || recordedAt > 8_640_000_000_000_000) return
        const entry: OperatorObservation = { source: 'operator', binding: requestBinding(selected), observer: observer.trim().slice(0, 64), note: note.trim().slice(0, 512), recordedAt }
        const next = [entry, ...observations].slice(0, NOTE_LIMIT)
        setObservations(next)
        setNote('')
        try {
          sessionStorage.setItem(STORAGE_PREFIX + state.sessionId, JSON.stringify(next))
          setStorageMessage('Operator observation recorded locally; it is not relay verification.')
        } catch {
          setStorageMessage('Observation is visible here, but browser storage is unavailable; it will be lost when this pane closes.')
        }
      }}>
        <label>Request with an outcome
          <select value={selected ? selectedBinding : ''} onChange={(event) => setSelectedBinding(event.target.value)}>
            <option value="">Choose a retained request</option>
            {terminal.map((request) => <option key={request.intent.intent_id} value={requestBinding(request)}>{request.intent.name} · {request.status} · {shortId(request.intent.intent_id)}</option>)}
          </select>
        </label>
        {selected && <p className="ct-mission-note">Request {selected.intent.intent_id}; source {selected.intent.source}; targets {selected.intent.selection.join(', ') || 'session'}. Captured connection epochs: {selected.intent.selection.map((id) => `${id}: ${selected.plan?.deviceEpochs?.[id] ?? 'unreported'}`).join(', ') || 'none'}.</p>}
        <label>Observer<input value={observer} maxLength={64} onChange={(event) => setObserver(event.target.value)} autoComplete="off" /></label>
        <label>Physical observation<textarea value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} placeholder="Describe what you personally observed, including any mismatch or lack of movement." /></label>
        <button type="submit" className="ct-mission-reset" disabled={!canRecord}>Record operator observation</button>
      </form>
      {storageMessage && <p role="status" className="ct-mission-note">{storageMessage}</p>}
      <ol className="ct-evidence-notes" aria-label="Unverified operator observations">
        {visibleNotes.map((entry, index) => {
          const request = terminal.find((candidate) => requestBinding(candidate) === entry.binding) as RequestRecord
          return <li key={`${entry.binding}:${entry.recordedAt}:${index}`}>
            <p><strong>Operator evidence · unverified</strong> · {entry.observer} · <time>{formatTime(entry.recordedAt)}</time></p>
            <p className="ct-mission-note">{request.intent.name} · {request.status} · {shortId(request.intent.intent_id)}</p>
            <p>{entry.note}</p>
          </li>
        })}
      </ol>
    </details>
  </section>
}
