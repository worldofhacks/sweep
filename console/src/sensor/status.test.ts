import { describe, expect, test } from 'vitest'
import { fixtureScenario } from '../testing/fixture-relay-client'
import { isFreshScan, sensorStatus, SENSOR_FRESH_MS } from './status'
import { createSensorStore } from './store'
import { mapDevices, scanningDevices } from '../modules/map/derive-map'

const now = 1_756_700_000_000
const fleet = fixtureScenario('mixed').fleet(now)
const robot = fleet[2]
const scan = { ...fixtureScenario('mixed').scans!(now)[2], v: 1 as const, session: 'sensors', event_id: 'scan-1' }

describe('sensor coverage honesty', () => {
  test('missing hardware, missing frames, stale frames and live scans remain distinct', () => {
    expect(sensorStatus(fleet[4], now)).toMatchObject({ tone: 'warn', text: expect.stringContaining('coverage unavailable') })
    expect(sensorStatus({ ...robot, sensor: undefined }, now).text).toContain('coverage unknown')
    expect(sensorStatus(robot, now).text).toBe('Lidar live · single scan plane only')
    expect(sensorStatus(robot, now + SENSOR_FRESH_MS).text).toContain('Lidar stale')
    expect(sensorStatus(robot, now - 1_000).text).toContain('freshness unknown')
    expect(isFreshScan(scan, scan.t + SENSOR_FRESH_MS)).toBe(true)
    expect(isFreshScan(scan, scan.t + SENSOR_FRESH_MS + 1)).toBe(false)
    expect(isFreshScan(scan, scan.t - 1)).toBe(false)
  })

  test('aircraft lidar is retained and placed; stale scans cannot supply a current pose', () => {
    const aircraft = { ...fleet[0], adapter_capabilities: ['flight', 'lidar'], telemetry: null }
    const store = createSensorStore()
    store.apply({ ...scan, drone_id: aircraft.drone_id })
    expect(scanningDevices([aircraft], store.snapshot())).toHaveLength(1)
    expect(mapDevices([aircraft], store.snapshot(), now)).toMatchObject([{ deviceClass: 'aircraft', source: 'scan' }])
    expect(mapDevices([aircraft], store.snapshot(), now + SENSOR_FRESH_MS)).toEqual([])
  })
})
