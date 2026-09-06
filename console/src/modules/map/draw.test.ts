import { describe, expect, test } from 'vitest'
import type { RelaySensorEvent } from '../../relay/contract'
import { callsNamed, recordingContext, type RecordedCall } from '../../testing/canvas-context'
import type { MapDevice } from './derive-map'
import { drawFleetMap, type FleetMapScene } from './draw'
import type { OccupancyMap } from './occupancy'
import { FALLBACK_PALETTE } from './palette'

const image = { width: 200, height: 160 } as unknown as CanvasImageSource

/** 200 × 160 cells at 0.05 m is 10 m × 8 m with its bottom-left corner at (-5, -4). */
const map: OccupancyMap = {
  image,
  resolution_m: 0.05,
  origin_x: -5,
  origin_y: -4,
  width: 200,
  height: 160,
  updated_at: 1_756_700_000_000,
}

const ranges = Array.from({ length: 360 }, () => 0)
ranges[0] = 200

const scan: RelaySensorEvent = {
  v: 1,
  t: 1_756_700_000_000,
  type: 'sensor',
  event_id: 'scan-1',
  session: 'map-draw-test',
  drone_id: 11,
  connection_epoch: 1,
  kind: 'lidar_scan',
  pose: { x: 1, y: 2, yaw_deg: 90 },
  angle_min_deg: 0,
  angle_increment_deg: 1,
  range_min_m: 0.15,
  range_max_m: 12,
  ranges_cm: ranges,
}

const device: MapDevice = {
  droneId: 11,
  label: 'G-01',
  deviceClass: 'ground_vehicle',
  unit: 1,
  x: 1,
  y: 2,
  headingDeg: 90,
  source: 'telemetry',
}

function scene(overrides: Partial<FleetMapScene> = {}): FleetMapScene {
  return {
    viewport: { width: 600, height: 400 },
    view: { centerX: 0, centerY: 0, scale: 50 },
    palette: FALLBACK_PALETTE,
    map,
    geofence: { min_x: -3, max_x: 3, min_y: -2, max_y: 2 },
    devices: [device],
    scans: [
      {
        unit: 1,
        scan,
        trail: [
          { x: 0, y: 2 },
          { x: 1, y: 2 },
        ],
      },
    ],
    ...overrides,
  }
}

function draw(overrides: Partial<FleetMapScene> = {}): RecordedCall[] {
  const context = recordingContext()
  drawFleetMap(context, scene(overrides))
  return context.calls
}

describe('fleet map draw passes', () => {
  test('the passes run in order: ground, raster, geofence, trails, returns, poses', () => {
    const names = draw().map((call) => call.name)
    const order = [
      names.indexOf('clearRect'),
      names.indexOf('drawImage'),
      names.indexOf('setLineDash'),
      names.indexOf('moveTo'),
      names.indexOf('fillRect', names.indexOf('drawImage')),
      names.indexOf('fillText'),
    ]
    expect(order).toEqual([...order].sort((left, right) => left - right))
    expect(order.every((index) => index >= 0)).toBe(true)
  })

  test('the raster covers the world rectangle its headers describe, with row 0 at maximum y', () => {
    const calls = draw()
    // Top-left of the image is (-5, 4) in the world: 10 m wide, 8 m high at 50 px/m.
    expect(callsNamed(calls, 'drawImage')).toEqual([[image, 50, 0, 500, 400]])
    expect(callsNamed(calls, 'set imageSmoothingEnabled')).toEqual([[false]])
    expect(callsNamed(calls, 'strokeRect')[0]).toEqual([50, 0, 500, 400])
  })

  test('the geofence is a dashed box in its own colour', () => {
    const calls = draw()
    expect(callsNamed(calls, 'setLineDash')).toEqual([[[6, 4]], [[]]])
    expect(callsNamed(calls, 'strokeRect')[1]).toEqual([150, 100, 300, 200])
    const dashed = calls.findIndex((call) => call.name === 'setLineDash')
    expect(calls[dashed - 2]).toEqual({ name: 'set strokeStyle', args: [FALLBACK_PALETTE.geofence] })
  })

  test('the trail is one polyline per device in its unit colour, at reduced opacity', () => {
    const calls = draw()
    expect(callsNamed(calls, 'moveTo')[0]).toEqual([300, 100])
    expect(callsNamed(calls, 'lineTo')[0]).toEqual([350, 100])
    expect(callsNamed(calls, 'set globalAlpha')).toEqual([[0.45], [1]])
    expect(calls.some((call) => call.name === 'set strokeStyle' && call.args[0] === '#2F7F9E')).toBe(true)
  })

  test('a return is drawn where the scan says it is: pose plus yaw plus bin angle', () => {
    const calls = draw()
    // The only return is 2 m along the forward axis of a device at (1, 2)
    // facing +y, so (1, 4) in the world and (350, 0) on the canvas.
    const dots = callsNamed(calls, 'fillRect').filter((args) => args[2] === 2 && args[3] === 2)
    expect(dots).toEqual([[349, -1, 2, 2]])
  })

  test('a device with a heading is a triangle pointing along it, labelled with its device id', () => {
    const calls = draw()
    // The pose is at (350, 100); the nose is nine pixels north of it.
    expect(callsNamed(calls, 'moveTo').at(-1)).toEqual([350, 91])
    const [left] = callsNamed(calls, 'lineTo').slice(-2)
    expect(left[0]).toBeCloseTo(345.835, 3)
    expect(left[1]).toBeCloseTo(104.964, 3)
    expect(callsNamed(calls, 'fillText')).toEqual([['G-01', 362, 91]])
  })

  test('a device with no reported heading is a circle rather than a guessed direction', () => {
    const calls = draw({ devices: [{ ...device, headingDeg: null }] })
    expect(callsNamed(calls, 'arc')).toEqual([[350, 100, 4.5, 0, Math.PI * 2]])
    expect(callsNamed(calls, 'moveTo')).toHaveLength(1) // the trail only
  })

  test('without a raster or a geofence only the fleet is drawn', () => {
    const calls = draw({ map: null, geofence: null })
    expect(callsNamed(calls, 'drawImage')).toEqual([])
    expect(callsNamed(calls, 'strokeRect')).toEqual([])
    expect(callsNamed(calls, 'fillText')).toEqual([['G-01', 362, 91]])
  })
})
