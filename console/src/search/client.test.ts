import { describe, expect, test, vi } from 'vitest'
import { HttpSearchClient } from './client'
import type { IntentV1 } from '../relay/contract'

const intent: IntentV1 = {
  v: 1,
  t: 100,
  type: 'intent',
  intent_id: 'search-1',
  retry_of: null,
  source: 'console',
  session: 'session-1',
  name: 'search',
  args: { zone_id: 'lobby', target_class: 'backpack' },
  selection: [1],
  mode: 'indoor',
  confirm: false,
}

function preview() {
  return {
    session: intent.session,
    intent_id: intent.intent_id,
    t: 100,
    expires_at_ms: 15_100,
    plan: { roster_version: 7, selection: [1], navigation: { route: {
      destination_zone_id: 'lobby', execution_order: [1], routes: [{
        drone: { drone_id: 1 }, arrival_slot: { slot_id: 'search-lobby-1' },
        waypoints: [{ x_m: 0, y_m: 0, z_m: 1 }, { x_m: 3, y_m: 2, z_m: 1 }],
      }],
    } } },
    preview: { zone_id: 'lobby', target_class: 'backpack', allocations: [
      { drone_id: 1, source_id: 'camera-1', task_id: 'task-1', workload_cells: 2, lane_count: 1 },
    ] },
  }
}

function status(acknowledged = false) {
  return {
    session: intent.session,
    intent_id: intent.intent_id,
    state: 'running',
    tasks: [{
      drone_id: 1, task_id: 'task-1', state: 'active', covered_cells: 1, total_cells: 2,
      covered_cell_ids: ['cell-1'], cells: [
        { cell_id: 'cell-1', x_m: 1, y_m: 1, z_m: 1, floor_id: 'floor-1' },
        { cell_id: 'cell-2', x_m: 2, y_m: 1, z_m: 1, floor_id: 'floor-1' },
      ],
    }],
    candidates: [{
      sighting_id: 'sighting/1', source_id: 'camera-1', acknowledged, label: 'backpack', confidence: 0.9,
      bbox_xyxy: [1, 2, 3, 4], observation_count: 2,
      frame: { frame_id: 'frame-1', source_id: 'camera-1', mission_id: 'mission-1', worker_run_id: null,
        frame_sequence: 4, decoded_at_monotonic_s: 2.1, evaluated_at_monotonic_s: 2.2 },
      position: { x_m: 1, y_m: 1, z_m: 1, zone_id: 'lobby', floor_id: 'floor-1' },
    }],
    detection_workers: [{ drone_id: 1, state: 'running', failure_reason: null }],
  }
}

describe('search HTTP client', () => {
  test('uses authenticated same-origin search endpoints and parses preview, status, and acknowledgement', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ session: intent.session, target_classes: ['backpack'], zones: ['lobby'] })))
      .mockResolvedValueOnce(new Response(JSON.stringify(preview())))
      .mockResolvedValueOnce(new Response(JSON.stringify(status())))
      .mockResolvedValueOnce(new Response(JSON.stringify(status(true))))
    const client = new HttpSearchClient({ baseUrl: 'wss://relay.example/ws', token: 'test-token' }, fetcher)

    await expect(client.catalog(intent.session)).resolves.toEqual({ target_classes: ['backpack'], zones: ['lobby'] })
    await expect(client.preview(intent)).resolves.toMatchObject({ intent_id: 'search-1', preview: { target_class: 'backpack' } })
    await expect(client.status(intent.session, intent.intent_id)).resolves.toMatchObject({ state: 'running', tasks: [{ covered_cells: 1 }] })
    await expect(client.acknowledge(intent.session, intent.intent_id, 'sighting/1')).resolves.toMatchObject({ candidates: [{ acknowledged: true }] })

    expect(fetcher.mock.calls[1][0]).toBe('https://relay.example/session/session-1/search/preview')
    expect(fetcher.mock.calls[1][1]).toMatchObject({ headers: { Authorization: 'Bearer test-token' } })
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toMatchObject({ intent: { confirm: true, name: 'search' } })
    expect(fetcher.mock.calls[3][0]).toBe('https://relay.example/session/session-1/search/search-1/findings/sighting%2F1/ack')
  })

  test('calls an injected browser fetcher as a plain function', async () => {
    const fetcher = vi.fn(function (this: unknown) {
      return Promise.resolve(
        new Response(
          JSON.stringify({ session: intent.session, target_classes: ['backpack'], zones: ['lobby'] }),
        ),
      )
    })
    const client = new HttpSearchClient(
      { baseUrl: 'wss://relay.example/ws', token: 'test-token' },
      fetcher as unknown as typeof fetch,
    )

    await expect(client.catalog(intent.session)).resolves.toEqual({
      target_classes: ['backpack'],
      zones: ['lobby'],
    })
    expect(fetcher.mock.contexts[0]).toBeUndefined()
  })
})

export { intent as searchIntent, preview as searchPreview, status as searchStatus }
