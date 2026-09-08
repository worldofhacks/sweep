import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createInitialControlState, type RequestRecord, type RequestStatus } from '../../control/state'
import { SessionRunEvidence } from './SessionRunEvidence'

const t = 1_756_700_000_000
const session = 'mission-evidence-test'

function request(id: string, status: RequestStatus, requestSession = session): RequestRecord {
  return {
    intent: { v: 1, t, type: 'intent', intent_id: id, retry_of: null, source: 'webcam', session: requestSession, name: 'formation_set', args: { name: 'line' }, selection: [1, 2], mode: 'indoor', confirm: true },
    status, timestamps: { draft: t - 1000, [status]: t },
    plan: { title: 'Line', steps: [], rosterVersion: 1, deviceEpochs: { 1: 3, 2: 5 } },
  }
}

function state(...requests: RequestRecord[]) {
  return { ...createInitialControlState(session, t), requests }
}

function count(label: string) {
  return within(screen.getByText(label).parentElement as HTMLElement).getByRole('definition').textContent
}

function annotate(note = 'Both aircraft remained stationary.') {
  const select = screen.getByLabelText('Request with an outcome') as HTMLSelectElement
  fireEvent.change(select, { target: { value: select.options[1].value } })
  fireEvent.change(screen.getByLabelText('Observer'), { target: { value: 'Alex' } })
  fireEvent.change(screen.getByLabelText('Physical observation'), { target: { value: note } })
  fireEvent.click(screen.getByRole('button', { name: 'Record operator observation' }))
}

beforeEach(() => sessionStorage.clear())

describe('truthful session run evidence', () => {
  test('empty state has no demo steps, pass claim, autoplay or fabricated outcomes', () => {
    render(<SessionRunEvidence state={state()} now={() => t} />)
    expect(screen.getByText(/No request evidence/)).toBeInTheDocument()
    expect(count('Relay-reported completed')).toBe('0')
    expect(screen.queryByText(/Pass —|zero unsafe|ten steps/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /start|run|play|complete/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(/Operator physical observations/))
    expect(screen.getByRole('button', { name: 'Record operator observation' })).toBeDisabled()
  })

  test('counts only this session and preserves exact distinctions between reported outcomes and drafts', () => {
    render(<SessionRunEvidence state={state(request('completed', 'completed'), request('failed', 'failed'), request('refused', 'refused'), request('draft', 'pending_confirmation'), request('sent', 'sent'), request('other', 'completed', 'other-session'))} />)
    expect(count('Relay-reported completed')).toBe('1')
    expect(count('Failed or refused')).toBe('2')
    expect(count('Drafts, cancellations or invalidations')).toBe('1')
    expect(count('Awaiting outcome')).toBe('1')
    expect(screen.getByText('Request outcomes do not prove physical movement.')).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /other/ })).not.toBeInTheDocument()
  })

  test('operator notes cannot complete a refused request and survive leaving the pane as unverified evidence', () => {
    const record = request('line-refused', 'refused')
    const current = state(record)
    const view = render(<SessionRunEvidence state={current} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/))
    annotate()
    expect(screen.getByText('Operator evidence · unverified')).toBeInTheDocument()
    expect(screen.getByText(/Captured connection epochs: 1: 3, 2: 5/)).toBeInTheDocument()
    expect(count('Relay-reported completed')).toBe('0')
    expect(count('Failed or refused')).toBe('1')
    expect(record.status).toBe('refused')
    view.unmount()
    render(<SessionRunEvidence state={current} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/))
    expect(screen.getByText('Both aircraft remained stationary.')).toBeInTheDocument()
    expect(screen.getByText('Operator evidence · unverified')).toBeInTheDocument()
  })

  test('notes and selected requests never carry into another session or a different captured epoch', () => {
    const record = request('reused-id', 'completed')
    const view = render(<SessionRunEvidence state={state(record)} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/)); annotate('I observed one short movement.')
    const differentEpoch = { ...record, plan: { ...record.plan!, deviceEpochs: { 1: 9, 2: 5 } } }
    view.rerender(<SessionRunEvidence state={state(differentEpoch)} now={() => t} />)
    expect(screen.queryByText('I observed one short movement.')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Request with an outcome')).toHaveValue('')
    view.rerender(<SessionRunEvidence state={{ ...state(record), sessionId: 'new-session' }} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/))
    expect(screen.queryByText('I observed one short movement.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record operator observation' })).toBeDisabled()
    expect(count('Relay-reported completed')).toBe('0')
  })

  test('a reused intent ID with a new timestamp does not inherit stored operator evidence', () => {
    const original = request('reused-after-reset', 'completed')
    const view = render(<SessionRunEvidence state={state(original)} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/)); annotate('Observed on the original request.')
    view.rerender(<SessionRunEvidence state={state({ ...original, intent: { ...original.intent, t: t + 1000 } })} now={() => t + 1000} />)
    expect(screen.queryByText('Observed on the original request.')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Request with an outcome')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Record operator observation' })).toBeDisabled()
  })

  test('unreported epochs stay unreported and removing the retained request removes its visible annotations', () => {
    const record = { ...request('no-plan', 'failed'), plan: undefined }
    const view = render(<SessionRunEvidence state={state(record)} now={() => t} />)
    fireEvent.click(screen.getByText(/Operator physical observations/)); annotate()
    expect(screen.getByText(/Captured connection epochs: 1: unreported, 2: unreported/)).toBeInTheDocument()
    view.rerender(<SessionRunEvidence state={state()} now={() => t} />)
    expect(screen.queryByText('Both aircraft remained stationary.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record operator observation' })).toBeDisabled()
  })

  test('browser storage failure is explicit and never presented as durable evidence', () => {
    render(<SessionRunEvidence state={state(request('failed', 'failed'))} now={() => t} />)
    vi.stubGlobal('sessionStorage', { getItem: () => null, setItem: () => { throw new Error('quota') } })
    fireEvent.click(screen.getByText(/Operator physical observations/)); annotate()
    expect(screen.getByRole('status')).toHaveTextContent('browser storage is unavailable')
    expect(count('Relay-reported completed')).toBe('0')
    vi.unstubAllGlobals()
  })

  test('malformed stored notes and non-finite timestamps do not create evidence', () => {
    sessionStorage.setItem('sweep.operator-observations.v1:' + session, '[{"source":"relay","note":"passed"}]')
    render(<SessionRunEvidence state={state(request('failed', 'failed'))} now={() => Infinity} />)
    fireEvent.click(screen.getByText(/Operator physical observations/)); annotate()
    expect(screen.queryByText('Operator evidence · unverified')).not.toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Unverified operator observations' })).toBeEmptyDOMElement()
  })
})
