import { describe, expect, test, vi } from 'vitest'
import { HttpSearchClient, type SearchPreview, type SearchStatus } from './client'
import type { IntentV1 } from '../relay/contract'

const intent: IntentV1 = { v: 1, t: 100, type: 'intent', intent_id: 'search-1', retry_of: null, source: 'console', session: 'session-1', name: 'search', args: { zone_id: 'lobby', target_class: 'backpack' }, selection: [1], mode: 'indoor', confirm: false }
export function searchPreview(): SearchPreview { return { session: intent.session, intent_id: intent.intent_id, t: 100, expiresAt: 15_100, routes: [{ drone_id: 1, connection_epoch: 1, frame: 'map_enu', waypoints: [[0,0,1], [1,0,1], [1,1,1]] }], preview: { zone_id: 'lobby', target_class: 'backpack', allocations: [{ drone_id: 1, source_id: 'camera-1', task_id: 'task-1', workload_cells: 2, lane_count: 1 }] } } }
export function searchStatus(acknowledged = false): SearchStatus { return { session: intent.session, intent_id: intent.intent_id, state: 'running', tasks: [{ drone_id: 1, task_id: 'task-1', state: 'active', covered_cells: 1, total_cells: 2, covered_cell_ids: ['cell-1'], cells: [{ cell_id: 'cell-1', x_m: 1, y_m: 1, z_m: 1, floor_id: 'floor-1' }] }], candidates: [{ sighting_id: 'sighting/1', source_id: 'camera-1', acknowledged, label: 'backpack', confidence: 0.9, bbox_xyxy: [1, 2, 3, 4], observation_count: 2, frame: { frame_id: 'frame-1', source_id: 'camera-1', mission_id: 'mission-1', worker_run_id: null, frame_sequence: 4, decoded_at_monotonic_s: 2.1, evaluated_at_monotonic_s: 2.2 }, position: { x_m: 1, y_m: 1, z_m: 1, zone_id: 'lobby', floor_id: 'floor-1' } }], detection_workers: [{ drone_id: 1, state: 'running', failure_reason: null }] } }
function previewResponse() { return { ...searchPreview(), expires_at_ms: searchPreview().expiresAt, preview: searchPreview().preview, plan: {}, type: 'search_preview' } }
describe('search HTTP client', () => {
  test('refuses status from a different mission and previews without the flight route', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...searchStatus(), intent_id: 'another-search' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...previewResponse(), routes: [] })))
    const client = new HttpSearchClient({ baseUrl: 'https://relay.example', token: 'test-token' }, fetcher)
    await expect(client.status(intent.session, intent.intent_id)).rejects.toThrow('invalid search status')
    await expect(client.preview(intent)).rejects.toThrow('invalid search preview')
  })

  test('uses authenticated endpoints and parses preview, status, and acknowledgement', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ session: intent.session, target_classes: ['backpack'], zones: ['lobby'] }))).mockResolvedValueOnce(new Response(JSON.stringify(previewResponse()))).mockResolvedValueOnce(new Response(JSON.stringify(searchStatus()))).mockResolvedValueOnce(new Response(JSON.stringify(searchStatus(true))))
    const client = new HttpSearchClient({ baseUrl: 'wss://relay.example/ws', token: 'test-token' }, fetcher)
    await expect(client.catalog(intent.session)).resolves.toEqual({ target_classes: ['backpack'], zones: ['lobby'] })
    await expect(client.preview(intent)).resolves.toEqual(searchPreview())
    await expect(client.status(intent.session, intent.intent_id)).resolves.toMatchObject({ state: 'running' })
    await expect(client.acknowledge(intent.session, intent.intent_id, 'sighting/1')).resolves.toMatchObject({ candidates: [{ acknowledged: true }] })
    expect(fetcher.mock.calls[1][0]).toBe('https://relay.example/session/session-1/search/preview')
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toMatchObject({ intent: { confirm: true, name: 'search' } })
    expect(fetcher.mock.calls[3][0]).toBe('https://relay.example/session/session-1/search/search-1/findings/sighting%2F1/ack')
  })
})
