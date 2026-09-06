import { describe, expect, test } from 'vitest'
import { controlReducer, createInitialControlState } from '../control/state'
import { publicNodeEvents } from '../testing/public-node-events'
import { parseRelayServerEvent } from './contract'

const [capabilities, status, readiness] = publicNodeEvents()

describe('public node event contract', () => {
  test('reads normalized capabilities, node status and capture readiness without granting control', () => {
    const initial = createInitialControlState('node-event-test', 0)
    let state = initial
    for (const raw of [capabilities, { ...status, control_authority: true }, { ...readiness, pose_ok: true }]) {
      const event = parseRelayServerEvent(raw)
      expect(event).toEqual(raw)
      if (!event) throw new Error('expected valid public event')
      state = controlReducer(state, { type: 'relay_event', event })
      expect(state.aircraft).toBe(initial.aircraft)
      expect(state.selection).toBe(initial.selection)
      expect(state.armed).toBe(false)
      expect(state.enabledIntentNames).toEqual([])
    }
    expect(state.seenEventIds).toHaveLength(3)
  })

  test('rejects missing, extra, signed, malformed identity and unknown event fields', () => {
    for (const valid of publicNodeEvents()) {
      for (const field of Object.keys(valid)) {
        const missing: Record<string, unknown> = { ...valid }
        delete missing[field]
        expect(parseRelayServerEvent(missing), `${valid.type} missing ${field}`).toBeNull()
      }
      for (const patch of [{ extra: true }, { signature: 'not-public' }, { drone_id: 0 }, { connection_epoch: 0 },
        { connection_epoch: 1.5 }, { t: -1 }, { session: '' }, { event_id: '' }, { type: 'unknown_node_event' }]) {
        expect(parseRelayServerEvent({ ...valid, ...patch })).toBeNull()
      }
    }
  })

  test('refuses invalid camera claims and bounded list/text violations', () => {
    for (const patch of [
      { photo_capture: 'false' }, { media_retrieval: 1 }, { gimbal_pitch_min_deg: 61 },
      { horizontal_fov_deg: 0 }, { horizontal_fov_deg: 361 }, { measured_hfov_deg: 180 },
      { storage_remaining_bytes: -1 }, { storage_remaining_bytes: 0.5 },
      { aircraft_model: ' unreported ' }, { sdk_version: '\u0000' }, { phone_model: 'é'.repeat(257) },
      { native_panorama_modes: ['pano', 'pano'] }, { native_panorama_modes: [''] },
      { native_panorama_modes: Array.from({ length: 65 }, (_, i) => `mode-${i}`) },
    ]) expect(parseRelayServerEvent({ ...capabilities, ...patch })).toBeNull()
  })

  test('refuses invalid phone health enums, battery values and nonboolean authority', () => {
    for (const patch of [
      { phone_battery_percent: -1 }, { phone_battery_percent: 101 }, { phone_battery_percent: 50.5 },
      { watchdog_state: 'ready' }, { watchdog_state: ['nominal'] }, { video_publish_state: 'streaming' },
      { phone_thermal_state: 'unknown' }, { control_authority: 1 }, { virtual_stick_enabled: 'false' },
      { authority_change_reason: 'Not Granted' },
    ]) expect(parseRelayServerEvent({ ...status, ...patch })).toBeNull()
  })

  test('reads finite guidance suggestions and refuses malformed readiness', () => {
    expect(parseRelayServerEvent({ ...readiness, guidance_mode: 'registered_metric', room_id: 'room-1',
      capture_id: 'capture-1', coverage_missing: [0, 90], next_heading_deg: 90,
      suggested_delta: { kind: 'yaw', degrees: -45 } })).not.toBeNull()
    for (const patch of [
      { pose_ok: 1 }, { guidance_mode: 'automatic' }, { guidance_mode: ['visual_advisory'] },
      { coverage_missing: [360] }, { coverage_missing: [0, 0] }, { coverage_missing: Array.from({ length: 9 }, (_, i) => i) },
      { next_heading_deg: -1 }, { suggested_delta: { kind: 'translate', degrees: 1 } },
      { suggested_delta: { kind: 'yaw', degrees: Infinity } },
      { suggested_delta: { kind: 'yaw', degrees: 1, execute: true } },
    ]) expect(parseRelayServerEvent({ ...readiness, ...patch })).toBeNull()
  })
})
