import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import type { RelayClient, RelayClientEvent, RelayClientListener } from '../relay/client'
import { C1_BASIC_CONTROL_INTENTS, C2_FLEET_OPERATIONS_INTENTS, isConsoleIntentV1, parseRelayServerEvent, type IntentV1, type RelayAcknowledgementEvent, type RelayServerEvent } from '../relay/contract'
import type { SurveyLifecycleRequest } from '../relay/survey'
import { fixtureAircraft } from '../testing/fixture-relay-client'
import { fieldGroundPose, fieldGroundState } from '../testing/field-ground'
import { GroundSurveyPane } from '../modules/control/GroundSurveyPane'
import { useControlConsole } from './use-control-console'
import type { SurveyCandidateClient, SurveyCandidatePreview } from '../platform/survey-client'
import fixture from '../testing/survey-candidate.json'
import { deferred } from '../modules/map/authoring/test-fixtures'
import { surveyLifecycleBlockedReason } from './survey'

const T = 1788790000000, SESSION = 'survey-console-test'
class ControlledRelay implements RelayClient {
  readonly transport = 'websocket' as const
  listeners = new Set<RelayClientListener>()
  sent: IntentV1[] = []
  lifecycle: SurveyLifecycleRequest[] = []
  failLifecycle = false
  start() { this.connection('connected') }
  stop() {}
  subscribe(listener: RelayClientListener) { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  emit(event: RelayClientEvent) { for (const listener of this.listeners) listener(event) }
  connection(status: 'connected' | 'disconnected') { this.emit({ kind: 'connection', connection: { status, transport: this.transport, changedAt: T } }) }
  server(event: RelayServerEvent) { this.emit({ kind: 'server_event', event }) }
  async sendIntent(intent: IntentV1) { this.sent.push(intent) }
  async sendSurveyLifecycle(frame: SurveyLifecycleRequest) { this.lifecycle.push(frame); if (this.failLifecycle) throw new Error('isolated transport failure') }
}
const readyState = (t: number) => fieldGroundState(t, SESSION, { enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'survey_area', 'ground_velocity'] })
function mount() {
  let now = T, next = 0
  const consoleClient = new ControlledRelay(), keyboard = new ControlledRelay(), webcam = new ControlledRelay()
  const dependencies = { now: () => now, nextId: () => `survey-${++next}` }
  const providers = new Map<ControlledRelay, { console: ControlledRelay; keyboard: ControlledRelay; webcam: ControlledRelay }>()
  const hook = renderHook(({ client }) => {
    if (!providers.has(client)) providers.set(client, { console: client, keyboard, webcam })
    return useControlConsole({ sessionId: SESSION, clients: providers.get(client)!, intentDependencies: dependencies })
  }, { initialProps: { client: consoleClient } })
  act(() => { consoleClient.server(readyState(T)); consoleClient.server(fieldGroundPose(T + 1, SESSION)); consoleClient.server(readyState(T + 2)) })
  const start = () => {
    act(() => { hook.result.current.issueIntent({ name: 'survey_area', args: { area_id: 'floor-1' } }) })
    const id = hook.result.current.pendingRequest!.intent.intent_id
    act(() => { hook.result.current.confirmRequest(id) })
    return id
  }
  return { ...hook, consoleClient, keyboard, webcam, start, advance: (delta: number) => { now += delta }, now: () => now }
}
function ack(intentId: string, patch: Partial<RelayAcknowledgementEvent> = {}): RelayAcknowledgementEvent {
  const value = parseRelayServerEvent({ v: 1, type: 'acknowledgement', event_id: `ack-${Math.random()}`, session: SESSION,
    t: T + 3, intent_id: intentId, command_id: null, source: 'survey_area', status: 'executing', reason: null, detail: null,
    drone_id: 11, connection_epoch: 3, roster_version: 100, result: { run_id: `run-${intentId}`, connection_epoch: 3 }, ...patch })
  if (value?.type !== 'acknowledgement') throw new Error('Invalid isolated survey ACK')
  return value
}

describe('confirmed console survey lifecycle', () => {
  test('a provider without lifecycle transport cannot start a recording it cannot close', () => {
    const h = mount(), legacy = new ControlledRelay()
    Object.defineProperty(legacy, 'sendSurveyLifecycle', { value: undefined })
    h.rerender({ client: legacy })
    act(() => { legacy.server(readyState(T + 3)); legacy.server(fieldGroundPose(T + 4, SESSION)); legacy.server(readyState(T + 5)) })
    act(() => { expect(h.result.current.issueIntent({ name: 'survey_area', args: { area_id: 'floor-1' } })).toBeNull() })
    expect(legacy.sent).toEqual([])
    render(<GroundSurveyPane controller={h.result.current} now={h.now} />)
    expect(screen.getByRole('button', { name: 'Preview survey recording' })).toBeDisabled()
    expect(screen.getByText('The console provider has no survey lifecycle transport.')).toBeInTheDocument()
  })
  test('only confirmed console start is sent; duplicate confirmation and lifecycle clicks never drive or repeat', () => {
    const h = mount()
    act(() => { expect(h.result.current.prepareIntent({ name: 'survey_area', args: { area_id: 'floor-1' } }, 'webcam')).toBeNull() })
    expect(h.consoleClient.sent).toEqual([])
    act(() => { h.result.current.issueIntent({ name: 'survey_area', args: { area_id: 'floor-1' } }) })
    const id = h.result.current.pendingRequest!.intent.intent_id
    expect(h.consoleClient.sent).toEqual([])
    expect(h.result.current.pendingRequest?.plan?.steps.join(' ')).toMatch(/no drive/i)
    act(() => { h.result.current.confirmRequest(id); h.result.current.confirmRequest(id) })
    expect(h.consoleClient.sent).toHaveLength(1)
    expect(h.consoleClient.sent[0]).toMatchObject({ source: 'console', name: 'survey_area', confirm: true, selection: [11], args: { area_id: 'floor-1' } })
    expect(isConsoleIntentV1(h.consoleClient.sent[0])).toBe(true)
    expect(h.result.current.sendSurveyLifecycle(id, 'complete')).toBe(false)
    act(() => { h.consoleClient.server(ack(id)) })
    act(() => { expect(h.result.current.sendSurveyLifecycle(id, 'complete')).toBe(true); expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(false) })
    expect(h.consoleClient.lifecycle).toEqual([{ v: 1, t: T, type: 'survey_lifecycle', event_id: 'survey-2', session: SESSION, operation: 'complete', intent_id: id, run_id: `run-${id}`, connection_epoch: 3 }])
    expect(h.result.current.state.requests[0].status).toBe('executing')
    expect(h.keyboard.sent).toEqual([]); expect(h.webcam.sent).toEqual([])
  })
  test.each(['selection', 'source', 'unit', 'epoch'] as const)('retires completion after %s changes while exact original-run cancellation remains possible', (change) => {
    const h = mount(), id = h.start()
    act(() => { h.consoleClient.server(ack(id)) })
    const changed = readyState(T + 4)
    if (change === 'selection') changed.selection = []
    if (change === 'source') changed.drones[0].ground_readiness = { source_id: 'other' }
    if (change === 'unit') changed.drones[0].unit = 2
    if (change === 'epoch') changed.drones[0].connection_epoch = 4
    act(() => { h.consoleClient.server(changed); h.consoleClient.server(readyState(T + 5)) })
    act(() => { expect(h.result.current.sendSurveyLifecycle(id, 'complete')).toBe(false); expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(true) })
    expect(h.consoleClient.lifecycle[0]).toMatchObject({ operation: 'cancel', run_id: `run-${id}`, connection_epoch: 3 })
  })
  test.each(['disconnect', 'provider'] as const)('a %s change cannot revive either closure action', (change) => {
    const h = mount(), id = h.start()
    act(() => { h.consoleClient.server(ack(id)) })
    if (change === 'disconnect') act(() => { h.consoleClient.connection('disconnected'); h.consoleClient.connection('connected') })
    else { h.rerender({ client: new ControlledRelay() }); h.rerender({ client: h.consoleClient }) }
    act(() => { expect(h.result.current.sendSurveyLifecycle(id, 'complete')).toBe(false); expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(false) })
    expect(h.consoleClient.lifecycle).toEqual([])
  })
  test('rejects wrong run/epoch/target and late executing ACKs after authoritative completion', () => {
    const h = mount(), id = h.start()
    act(() => { h.consoleClient.server(ack(id)); h.consoleClient.server(ack(id, { result: { run_id: 'other', connection_epoch: 3 } })) })
    expect(h.result.current.state.requests[0].surveyRun?.runId).toBe(`run-${id}`)
    act(() => { h.consoleClient.server(ack(id, { drone_id: 12 })); h.consoleClient.server(ack(id, { connection_epoch: 4, result: { run_id: 'changed', connection_epoch: 4 } })) })
    const candidateId = `candidate-${'a'.repeat(32)}`
    act(() => { h.consoleClient.server(ack(id, { t: T + 4, status: 'completed', result: { run_id: `run-${id}`, connection_epoch: 3, candidate_id: candidateId } })); h.consoleClient.server(ack(id, { t: T + 5 })) })
    expect(h.result.current.state.requests[0]).toMatchObject({ status: 'completed', surveyCandidateId: candidateId, surveyRun: { runId: `run-${id}` } })
  })
  test.each([15_000, -1])('missing lifecycle receipt after time change %s remains unknown without repeat', (delta) => {
    const h = mount(), id = h.start()
    act(() => { h.consoleClient.server(ack(id)); h.result.current.sendSurveyLifecycle(id, 'cancel') })
    // ACK must reach the controller before the first closure action.
    if (!h.consoleClient.lifecycle.length) act(() => { h.result.current.sendSurveyLifecycle(id, 'cancel') })
    h.advance(delta)
    expect(surveyLifecycleBlockedReason(h.result.current.state, h.result.current.state.requests[0], h.now(), 'cancel')).toMatch(/unknown/)
    expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(false)
    expect(h.consoleClient.lifecycle).toHaveLength(1)
  })
  test('completion rechecks elapsed pose freshness even without a render; cancellation still only closes recording', () => {
    const h = mount(), id = h.start()
    act(() => { h.consoleClient.server(ack(id)) })
    h.advance(6_000)
    act(() => { expect(h.result.current.sendSurveyLifecycle(id, 'complete')).toBe(false); expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(true) })
    expect(h.consoleClient.lifecycle[0].operation).toBe('cancel')
  })
  test('failed transport or a missing receipt remains unknown and cannot automatically retry', async () => {
    const h = mount(), id = h.start()
    h.consoleClient.failLifecycle = true
    act(() => { h.consoleClient.server(ack(id)) })
    await act(async () => { h.result.current.sendSurveyLifecycle(id, 'cancel') })
    expect(surveyLifecycleBlockedReason(h.result.current.state, h.result.current.state.requests[0], T, 'cancel')).toMatch(/unknown/)
    expect(h.result.current.sendSurveyLifecycle(id, 'cancel')).toBe(false)
    expect(h.consoleClient.lifecycle).toHaveLength(1)
  })
  test('Ground flow shows truthful status and explicit completion, with no invented stored count', () => {
    const h = mount()
    const view = render(<GroundSurveyPane controller={h.result.current} now={h.now} />)
    fireEvent.change(screen.getByLabelText('Survey area ID'), { target: { value: 'floor-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview survey recording' }))
    expect(h.consoleClient.sent).toEqual([])
    const id = h.result.current.pendingRequest!.intent.intent_id
    act(() => { h.result.current.confirmRequest(id); h.consoleClient.server(ack(id)) })
    view.rerender(<GroundSurveyPane controller={h.result.current} now={h.now} />)
    expect(screen.getByText('Relay acknowledged recording')).toBeInTheDocument()
    expect(screen.getByText(/Stored scan count and percent complete are not reported/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel recording' }))
    view.rerender(<GroundSurveyPane controller={h.result.current} now={h.now} />)
    expect(screen.getByRole('button', { name: 'Cancel recording' })).toBeDisabled()
    act(() => { h.consoleClient.server(ack(id, { t: T + 4, status: 'invalidated', result: undefined, reason: 'survey_cancelled' })) })
    view.rerender(<GroundSurveyPane controller={h.result.current} now={h.now} />)
    expect(screen.getByText('Relay confirmed recording cancellation')).toBeInTheDocument()
    expect(h.consoleClient.sent.map((intent) => intent.name)).toEqual(['survey_area'])
  })
})

function completeForCandidate(h: ReturnType<typeof mount>): SurveyCandidatePreview {
  const id = h.start(), candidateId = fixture.candidate_id
  act(() => { h.consoleClient.server(ack(id)); h.consoleClient.server(ack(id, { status: 'completed', t: T + 4, result: { candidate_id: candidateId, run_id: `run-${id}`, connection_epoch: 3 } })) })
  return { reference: { candidateId, session: SESSION, intentId: id, runId: `run-${id}`, deviceId: 11, connectionEpoch: 3, areaId: 'floor-1' },
    image: { dataUrl: `data:image/png;base64,${fixture.image.data_base64}`, width: 22, height: 2, sha256: fixture.image.sha256, bytes: fixture.image.bytes },
    frame: 'odom', resolutionM: 0.05, originXM: -0.05, originYM: -0.05, createdAt: T, source: fixture.source, evidence: fixture }
}

describe('candidate handoff provider lifetime', () => {
  test.each(['reconnect', 'provider'] as const)('pending %s change aborts old bytes and a restored provider can request fresh evidence', async (change) => {
    const h = mount(), candidate = completeForCandidate(h)
    const old = deferred<SurveyCandidatePreview>()
    const load = vi.fn<SurveyCandidateClient['load']>().mockReturnValueOnce(old.promise).mockResolvedValueOnce(candidate)
    const provider: SurveyCandidateClient = { load }, other: SurveyCandidateClient = { load: vi.fn() }
    const element = (client = provider) => <GroundSurveyPane controller={h.result.current} now={h.now} services={{ surveyCandidates: client }} />
    const view = render(element())
    fireEvent.click(screen.getByRole('button', { name: 'Load recorded candidate' }))
    const signal = load.mock.calls[0][1]!
    if (change === 'reconnect') {
      act(() => { h.consoleClient.connection('disconnected') }); view.rerender(element())
      act(() => { h.consoleClient.connection('connected') }); view.rerender(element())
    } else { view.rerender(element(other)); view.rerender(element(provider)) }
    expect(signal.aborted).toBe(true)
    expect(screen.getByRole('button', { name: 'Load recorded candidate' })).toBeEnabled()
    await act(async () => { old.resolve(candidate) })
    expect(screen.queryByAltText('Recorded local occupancy candidate')).not.toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Load recorded candidate' })) })
    expect(load).toHaveBeenCalledTimes(2)
    expect(screen.getByAltText('Recorded local occupancy candidate')).toBeInTheDocument()
    expect(screen.getByText(/Local frame: odom · unregistered · no navigation authority/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download editable Map draft' })).toBeEnabled()
    expect(h.consoleClient.sent.map((intent) => intent.name)).toEqual(['survey_area'])
  })
  test('loaded evidence is retired across provider A→B→A rather than revived', async () => {
    const h = mount(), candidate = completeForCandidate(h)
    const provider: SurveyCandidateClient = { load: vi.fn().mockResolvedValue(candidate) }
    const element = (client: SurveyCandidateClient) => <GroundSurveyPane controller={h.result.current} now={h.now} services={{ surveyCandidates: client }} />
    const view = render(element(provider))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Load recorded candidate' })) })
    expect(screen.getByAltText('Recorded local occupancy candidate')).toBeInTheDocument()
    view.rerender(element({ load: vi.fn() })); view.rerender(element(provider))
    expect(screen.queryByAltText('Recorded local occupancy candidate')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Load recorded candidate' })).toBeEnabled()
  })
})


describe('formation confirmation profile binding', () => {
  test.each(['formation_set', 'formation_next'] as const)('retires %s when a C2 shape preview becomes line-only', (name) => {
    const h = mount()
    const state = readyState(T + 5)
    state.selection = [1, 2]
    state.armed = true
    state.drones = fixtureAircraft(T + 5).slice(0, 2)
    state.capability_profile = 'c2_fleet_operations'
    state.enabled_intent_names = [...C2_FLEET_OPERATIONS_INTENTS]
    act(() => { h.consoleClient.server(state) })
    act(() => { h.result.current.prepareIntent(name === 'formation_set' ? { name, args: { name: 'column' } } : { name, args: {} }) })
    const id = h.result.current.pendingRequest!.intent.intent_id
    act(() => { h.consoleClient.server({ ...state, t: T + 6, event_id: 'shape-policy-changed', capability_profile: 'c1_basic_control.ground_mapped_line' }) })
    act(() => { expect(h.result.current.confirmRequest(id)).toBeNull() })
    expect(h.consoleClient.sent).toEqual([])
    expect(h.result.current.state.requests[0]).toMatchObject({ status: 'invalidated', reasonCode: 'formation_readiness_changed' })
  })
})
