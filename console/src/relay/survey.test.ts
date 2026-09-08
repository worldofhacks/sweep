import { describe, expect, test } from 'vitest'
import { WebSocketRelayClient } from './client'
import { isConsoleIntentV1, parseRelayServerEvent } from './contract'
import { isSurveyLifecycleRequest, type SurveyLifecycleRequest } from './survey'
class Socket extends EventTarget {
  readyState = 1
  sent: string[] = []
  send(value: string) { this.sent.push(value) }
  close() {}
  open() { this.dispatchEvent(new Event('open')) }
  message(value: object) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })) }
}
const frame: SurveyLifecycleRequest = { v: 1, type: 'survey_lifecycle', t: 100, event_id: 'event-1', session: 'session-test', operation: 'complete', intent_id: 'intent-1', run_id: 'run-1', connection_epoch: 1 }
describe('authenticated survey wire contracts', () => {
  test.each(['console', 'keyboard', 'webcam', 'language'] as const)('only the authenticated console source can send lifecycle frames (%s)', async (source) => {
    const socket = new Socket()
    const client = new WebSocketRelayClient({ baseUrl: 'wss://relay.test', sessionId: frame.session, token: 'isolated-token', source }, { now: () => 100, createSocket: () => socket as unknown as WebSocket })
    client.start(); socket.open()
    await expect(client.sendSurveyLifecycle(frame)).rejects.toThrow()
    socket.message({ v: 1, t: 100, type: 'auth.accepted', event_id: 'auth', session: frame.session, source, drone_id: null })
    if (source === 'console') {
      await client.sendSurveyLifecycle(frame)
      expect(JSON.parse(socket.sent[1])).toEqual(frame)
      await expect(client.sendSurveyLifecycle({ ...frame, session: 'other' })).rejects.toThrow()
      client.stop()
      await expect(client.sendSurveyLifecycle(frame)).rejects.toThrow()
    } else await expect(client.sendSurveyLifecycle(frame)).rejects.toThrow()
    expect(socket.sent.filter((text) => JSON.parse(text).type === 'survey_lifecycle')).toHaveLength(source === 'console' ? 1 : 0)
  })
  test('requires exact bounded lifecycle identity and no extra motion fields', () => {
    expect(isSurveyLifecycleRequest(frame)).toBe(true)
    for (const patch of [{ session: '' }, { operation: 'drive' }, { run_id: ' bad ' }, { connection_epoch: 0 }, { connection_epoch: 2 ** 31 }, { dx: 1 }, { t: NaN }]) expect(isSurveyLifecycleRequest({ ...frame, ...patch })).toBe(false)
  })
  test('survey start is exact confirmed console-only; mapped line needs confirmed webcam source', () => {
    const intent = { v: 1, t: 100, type: 'intent', intent_id: 'intent-1', retry_of: null, session: frame.session, source: 'console', name: 'survey_area', args: { area_id: 'area-1' }, selection: [11], mode: 'indoor', confirm: true }
    expect(isConsoleIntentV1(intent)).toBe(true)
    for (const patch of [{ confirm: false }, { source: 'webcam' }, { source: 'language' }, { selection: [11, 12] }, { args: { area_id: '' } }, { args: { area_id: 'area', dx: 1 } }]) expect(isConsoleIntentV1({ ...intent, ...patch })).toBe(false)
    const line = { ...intent, source: 'webcam', name: 'formation_set', args: { name: 'line' }, selection: [1, 2] }
    expect(isConsoleIntentV1(line)).toBe(true)
    expect(isConsoleIntentV1({ ...line, confirm: false })).toBe(false)
    expect(isConsoleIntentV1({ ...line, args: { name: 'column' } })).toBe(false)
  })
  test('accepts exact start/completed results only on corresponding survey acknowledgements', () => {
    const base = { v: 1, t: 100, type: 'acknowledgement', event_id: 'ack', session: frame.session, intent_id: frame.intent_id, command_id: null, source: 'survey_area', drone_id: 11, connection_epoch: 1, roster_version: 2, status: 'executing', reason: null, detail: null, result: { run_id: 'run-1', connection_epoch: 1 } }
    expect(parseRelayServerEvent(base)).not.toBeNull()
    const complete = { ...base, status: 'completed', result: { ...base.result, candidate_id: `candidate-${'a'.repeat(32)}` } }
    expect(parseRelayServerEvent(complete)).not.toBeNull()
    for (const patch of [{ source: 'adapter' }, { drone_id: null }, { command_id: 'cmd-1' }, { connection_epoch: 2 }, { status: 'accepted' }, { result: { ...base.result, arbitrary: true } }]) expect(parseRelayServerEvent({ ...base, ...patch })).toBeNull()
    expect(parseRelayServerEvent({ ...complete, status: 'executing' })).toBeNull()
    expect(parseRelayServerEvent({ ...complete, result: base.result })).toBeNull()
  })
})
