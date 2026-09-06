import { describe, expect, test } from 'vitest'
import type { RelaySensorEvent } from '../relay/contract'
import { SENSOR_TRAIL_LENGTH, createSensorStore } from './store'

function scan(overrides: Partial<RelaySensorEvent> = {}): RelaySensorEvent {
  return {
    v: 1,
    t: 1_756_700_000_000,
    type: 'sensor',
    event_id: 'scan-1',
    session: 'sensor-store-test',
    drone_id: 11,
    connection_epoch: 3,
    kind: 'lidar_scan',
    pose: { x: 1.2, y: -0.4, yaw_deg: 87.5 },
    angle_min_deg: 0,
    angle_increment_deg: 2,
    range_min_m: 0.15,
    range_max_m: 12,
    ranges_cm: Array.from({ length: 180 }, (_, index) => (index % 7 === 0 ? 0 : 150 + index)),
    ...overrides,
  }
}

describe('sensor store', () => {
  test('keeps the latest scan per device and a trail of the last twenty, oldest first', () => {
    const store = createSensorStore()
    let notified = 0
    store.subscribe(() => {
      notified += 1
    })
    for (let index = 0; index < SENSOR_TRAIL_LENGTH + 5; index += 1) {
      expect(store.apply(scan({ event_id: `scan-${index}`, t: 1_756_700_000_000 + index * 200 }))).toBe(true)
    }
    store.apply(scan({ drone_id: 12, event_id: 'other-1', t: 1_756_700_000_100 }))

    const snapshot = store.snapshot()
    expect(snapshot.latest[11].event_id).toBe(`scan-${SENSOR_TRAIL_LENGTH + 4}`)
    expect(snapshot.trails[11]).toHaveLength(SENSOR_TRAIL_LENGTH)
    expect(snapshot.trails[11][0].event_id).toBe('scan-5')
    expect(snapshot.trails[11].at(-1)?.event_id).toBe(`scan-${SENSOR_TRAIL_LENGTH + 4}`)
    expect(snapshot.latest[12].event_id).toBe('other-1')
    expect(snapshot.trails[12]).toHaveLength(1)
    expect(notified).toBe(SENSOR_TRAIL_LENGTH + 6)
  })

  test('ignores a repeated or older frame and starts a fresh trail on a new connection epoch', () => {
    const store = createSensorStore()
    store.apply(scan({ event_id: 'a', t: 1_756_700_000_400 }))
    const before = store.snapshot()
    expect(store.apply(scan({ event_id: 'a', t: 1_756_700_000_400 }))).toBe(false)
    expect(store.apply(scan({ event_id: 'b', t: 1_756_700_000_200 }))).toBe(false)
    expect(store.snapshot()).toBe(before)

    expect(store.apply(scan({ event_id: 'c', t: 1_756_700_000_100, connection_epoch: 4 }))).toBe(true)
    expect(store.snapshot().trails[11].map((item) => item.event_id)).toEqual(['c'])
    expect(store.snapshot().latest[11].connection_epoch).toBe(4)
  })

  test('reset empties the store once and notifies only when something was held', () => {
    const store = createSensorStore()
    let notified = 0
    store.subscribe(() => {
      notified += 1
    })
    store.reset()
    expect(notified).toBe(0)
    store.apply(scan())
    store.reset()
    expect(store.snapshot()).toEqual({ latest: {}, trails: {} })
    expect(notified).toBe(2)
  })
})
