import { describe, expect, test } from 'vitest'
import type { RelaySensorEvent } from '../../relay/contract'
import {
  MAX_SCALE,
  MIN_SCALE,
  canvasToWorld,
  clampScale,
  fitView,
  panView,
  scanPoints,
  scanReturns,
  worldToCanvas,
  zoomView,
  type MapView,
  type Viewport,
} from './projection'

const viewport: Viewport = { width: 600, height: 400 }
const view: MapView = { centerX: 0, centerY: 0, scale: 50 }

function scan(overrides: Partial<RelaySensorEvent> = {}): RelaySensorEvent {
  return {
    v: 1,
    t: 1_756_700_000_000,
    type: 'sensor',
    event_id: 'scan-1',
    session: 'map-projection-test',
    drone_id: 11,
    connection_epoch: 1,
    kind: 'lidar_scan',
    pose: { x: 1, y: 2, yaw_deg: 90 },
    angle_min_deg: 0,
    angle_increment_deg: 1,
    range_min_m: 0.15,
    range_max_m: 12,
    ranges_cm: Array.from({ length: 360 }, () => 0),
    ...overrides,
  }
}

describe('map projection', () => {
  test('the world centre is the canvas centre and +y is up', () => {
    expect(worldToCanvas(view, viewport, 0, 0)).toEqual({ x: 300, y: 200 })
    expect(worldToCanvas(view, viewport, 2, 1)).toEqual({ x: 400, y: 150 })
    expect(worldToCanvas(view, viewport, -1, -2)).toEqual({ x: 250, y: 300 })
  })

  test('canvasToWorld inverts worldToCanvas', () => {
    const at = worldToCanvas(view, viewport, -3.25, 4.5)
    expect(canvasToWorld(view, viewport, at.x, at.y)).toEqual({ x: -3.25, y: 4.5 })
  })

  test('a drag moves the world under the pointer by the same pixels', () => {
    const panned = panView(view, 50, -100)
    expect(worldToCanvas(panned, viewport, 0, 0)).toEqual({ x: 350, y: 100 })
  })

  test('zoom keeps the world point under the cursor and clamps the scale', () => {
    const anchor = { x: 120, y: 340 }
    const before = canvasToWorld(view, viewport, anchor.x, anchor.y)
    const zoomed = zoomView(view, viewport, 2, anchor.x, anchor.y)
    expect(zoomed.scale).toBe(100)
    const after = canvasToWorld(zoomed, viewport, anchor.x, anchor.y)
    expect(after.x).toBeCloseTo(before.x, 10)
    expect(after.y).toBeCloseTo(before.y, 10)
    expect(zoomView(view, viewport, 1000, anchor.x, anchor.y).scale).toBe(MAX_SCALE)
    expect(zoomView(view, viewport, 0.0001, anchor.x, anchor.y).scale).toBe(MIN_SCALE)
    expect(clampScale(Number.NaN)).toBe(48)
  })

  test('fit centres a box and scales it to the smaller axis inside the margin', () => {
    const fitted = fitView({ min_x: -3, max_x: 3, min_y: -2, max_y: 2 }, viewport, 20)
    expect(fitted.centerX).toBe(0)
    expect(fitted.centerY).toBe(0)
    // 560 px across 6 m is 93.3; 360 px across 4 m is 90, and the smaller wins.
    expect(fitted.scale).toBeCloseTo(90, 6)
  })

  test('a scan bin is placed at the pose yaw plus the bin angle, counter-clockwise from +x', () => {
    const ranges = Array.from({ length: 360 }, () => 0)
    ranges[0] = 200 // 2 m along the device's forward axis
    ranges[90] = 100 // 1 m, 90 degrees counter-clockwise of it
    const points = scanPoints(scan({ ranges_cm: ranges }))
    expect(points).toHaveLength(2)
    // Pose (1, 2) facing +y: forward is north, 90 degrees more is west.
    expect(points[0].x).toBeCloseTo(1, 10)
    expect(points[0].y).toBeCloseTo(4, 10)
    expect(points[1].x).toBeCloseTo(0, 10)
    expect(points[1].y).toBeCloseTo(2, 10)
  })

  test('zero ranges and ranges outside the sensor bounds are not returns', () => {
    const ranges = Array.from({ length: 360 }, () => 0)
    ranges[10] = 0 // no return
    ranges[20] = 10 // 0.1 m, inside range_min_m
    ranges[30] = 1300 // 13 m, beyond range_max_m
    ranges[40] = 250
    const returns = scanReturns(scan({ ranges_cm: ranges }))
    expect(returns).toEqual([{ angleDeg: 40, rangeM: 2.5 }])
  })

  test('the bin angle follows angle_min_deg and the increment', () => {
    const ranges = Array.from({ length: 180 }, () => 0)
    ranges[3] = 150
    const returns = scanReturns(scan({ angle_min_deg: 10, angle_increment_deg: 2, ranges_cm: ranges }))
    expect(returns).toEqual([{ angleDeg: 16, rangeM: 1.5 }])
  })
})
