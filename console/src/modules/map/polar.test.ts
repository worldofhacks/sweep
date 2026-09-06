import { describe, expect, test } from 'vitest'
import type { RelaySensorEvent } from '../../relay/contract'
import { callsNamed, recordingContext } from '../../testing/canvas-context'
import { FALLBACK_PALETTE } from './palette'
import { POLAR_RINGS, drawPolarScan, polarPoint, polarRange } from './polar'

function scan(overrides: Partial<RelaySensorEvent> = {}): RelaySensorEvent {
  return {
    v: 1,
    t: 1_756_700_000_000,
    type: 'sensor',
    event_id: 'polar-1',
    session: 'polar-test',
    drone_id: 11,
    connection_epoch: 1,
    kind: 'lidar_scan',
    pose: { x: 0, y: 0, yaw_deg: 0 },
    angle_min_deg: 0,
    angle_increment_deg: 1,
    range_min_m: 0.15,
    range_max_m: 12,
    ranges_cm: Array.from({ length: 360 }, () => 0),
    ...overrides,
  }
}

describe('polar lidar plot', () => {
  test('forward is up and angles increase counter-clockwise', () => {
    // A 100 px plot has a 44 px radius inside its six-pixel edge.
    expect(polarPoint(100, 0, 4, 4)).toEqual({ x: 50, y: 6 })
    const left = polarPoint(100, 90, 4, 4)
    expect(left.x).toBeCloseTo(6, 10)
    expect(left.y).toBeCloseTo(50, 10)
    const behind = polarPoint(100, 180, 4, 4)
    expect(behind.x).toBeCloseTo(50, 10)
    expect(behind.y).toBeCloseTo(94, 10)
    const right = polarPoint(100, 270, 4, 4)
    expect(right.x).toBeCloseTo(94, 10)
    expect(right.y).toBeCloseTo(50, 10)
  })

  test('the radius is the fraction of the plotted range, and beyond it is clamped', () => {
    expect(polarPoint(100, 0, 2, 4).y).toBeCloseTo(28, 10)
    expect(polarPoint(100, 0, 8, 4).y).toBeCloseTo(6, 10)
    expect(polarPoint(100, 0, 0, 0)).toEqual({ x: 50, y: 50 })
  })

  test('the plotted range is the farthest return, never under a metre, never past the sensor', () => {
    const ranges = Array.from({ length: 360 }, () => 0)
    ranges[0] = 320
    ranges[10] = 150
    expect(polarRange(scan({ ranges_cm: ranges }))).toBe(3.2)
    expect(polarRange(scan())).toBe(1)
    // The 3.2 m return is beyond a 2 m sensor, so it is not a return at all.
    expect(polarRange(scan({ ranges_cm: ranges, range_max_m: 2 }))).toBe(1.5)
  })

  test('rings, the forward axis, then one dot per return in the device colour', () => {
    const ranges = Array.from({ length: 360 }, () => 0)
    ranges[0] = 200
    ranges[90] = 100
    const context = recordingContext()
    drawPolarScan(context, {
      size: 100,
      palette: FALLBACK_PALETTE,
      colour: FALLBACK_PALETTE.scans[1],
      scan: scan({ ranges_cm: ranges }),
    })
    const calls = context.calls
    expect(callsNamed(calls, 'arc')).toHaveLength(POLAR_RINGS.length)
    expect(callsNamed(calls, 'lineTo')).toEqual([[50, 6]])
    expect(calls.some((call) => call.name === 'set fillStyle' && call.args[0] === '#B4622B')).toBe(true)
    const dots = callsNamed(calls, 'fillRect').filter((args) => args[2] === 2)
    // Two metres forward fills the plot; one metre to the left is half of it.
    expect(dots[0]).toEqual([49, 5, 2, 2])
    expect(dots[1][0]).toBeCloseTo(27, 10)
    expect(dots[1][1]).toBeCloseTo(49, 10)
  })
})
