import type { RelaySensorEvent, SweepBox } from '../../relay/contract'
import type { MapDevice } from './derive-map'
import type { OccupancyMap } from './occupancy'
import { scanColor, type MapPalette } from './palette'
import { scanPoints, worldToCanvas, type MapView, type Point, type Viewport } from './projection'

/** One device's returns and the poses its recent scans were taken from. */
export interface MapScan {
  unit: number
  scan: RelaySensorEvent
  trail: readonly Point[]
}

export interface FleetMapScene {
  viewport: Viewport
  view: MapView
  palette: MapPalette
  /** The relay's occupancy raster, or null when there is none to draw. */
  map: OccupancyMap | null
  geofence: SweepBox | null
  devices: readonly MapDevice[]
  scans: readonly MapScan[]
}

const POSE_RADIUS = 9
const SCAN_DOT = 2
const LABEL_FONT = "11px 'IBM Plex Mono', monospace"

/**
 * One canvas, one ordered set of passes: the occupancy raster, the geofence,
 * the scan trails, the scan returns, then the device poses on top. Everything
 * is drawn in CSS pixels; the caller has already applied the device pixel
 * ratio.
 */
export function drawFleetMap(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { viewport, palette } = scene
  ctx.clearRect(0, 0, viewport.width, viewport.height)
  ctx.fillStyle = palette.ground
  ctx.fillRect(0, 0, viewport.width, viewport.height)
  drawRaster(ctx, scene)
  drawGeofence(ctx, scene)
  drawTrails(ctx, scene)
  drawScans(ctx, scene)
  drawDevices(ctx, scene)
}

/**
 * Image row 0 is the grid's maximum y and the origin headers name its
 * bottom-left corner, so the raster's top-left in the world is
 * (origin_x, origin_y + height × resolution).
 */
function drawRaster(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { map, view, viewport, palette } = scene
  if (!map) return
  const metresWide = map.width * map.resolution_m
  const metresHigh = map.height * map.resolution_m
  const topLeft = worldToCanvas(view, viewport, map.origin_x, map.origin_y + metresHigh)
  ctx.imageSmoothingEnabled = false
  ctx.drawImage(map.image, topLeft.x, topLeft.y, metresWide * view.scale, metresHigh * view.scale)
  ctx.strokeStyle = palette.frame
  ctx.lineWidth = 1
  ctx.strokeRect(topLeft.x, topLeft.y, metresWide * view.scale, metresHigh * view.scale)
}

function drawGeofence(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { geofence, view, viewport, palette } = scene
  if (!geofence) return
  const topLeft = worldToCanvas(view, viewport, geofence.min_x, geofence.max_y)
  const bottomRight = worldToCanvas(view, viewport, geofence.max_x, geofence.min_y)
  ctx.strokeStyle = palette.geofence
  ctx.lineWidth = 1.5
  ctx.setLineDash([6, 4])
  ctx.strokeRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y)
  ctx.setLineDash([])
}

function drawTrails(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { view, viewport, palette } = scene
  ctx.lineWidth = 1.5
  for (const { unit, trail } of scene.scans) {
    if (trail.length < 2) continue
    ctx.strokeStyle = scanColor(palette, unit)
    ctx.globalAlpha = 0.45
    ctx.beginPath()
    trail.forEach((point, index) => {
      const at = worldToCanvas(view, viewport, point.x, point.y)
      if (index === 0) ctx.moveTo(at.x, at.y)
      else ctx.lineTo(at.x, at.y)
    })
    ctx.stroke()
    ctx.globalAlpha = 1
  }
}

/** Hundreds of returns per device: square dots, not arcs. */
function drawScans(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { view, viewport, palette } = scene
  for (const { unit, scan } of scene.scans) {
    ctx.fillStyle = scanColor(palette, unit)
    for (const point of scanPoints(scan)) {
      const at = worldToCanvas(view, viewport, point.x, point.y)
      ctx.fillRect(at.x - SCAN_DOT / 2, at.y - SCAN_DOT / 2, SCAN_DOT, SCAN_DOT)
    }
  }
}

/**
 * A heading triangle per device, or a circle when nothing reports a heading;
 * the label is the device id the rest of the console uses.
 */
function drawDevices(ctx: CanvasRenderingContext2D, scene: FleetMapScene): void {
  const { view, viewport, palette } = scene
  ctx.font = LABEL_FONT
  ctx.textBaseline = 'middle'
  for (const device of scene.devices) {
    const at = worldToCanvas(view, viewport, device.x, device.y)
    ctx.fillStyle = palette.ink
    ctx.strokeStyle = palette.ground
    ctx.lineWidth = 2
    if (device.headingDeg === null) {
      ctx.beginPath()
      ctx.arc(at.x, at.y, POSE_RADIUS * 0.5, 0, Math.PI * 2)
      ctx.stroke()
      ctx.fill()
    } else {
      const nose = offset(at, device.headingDeg, POSE_RADIUS)
      const left = offset(at, device.headingDeg + 140, POSE_RADIUS * 0.72)
      const right = offset(at, device.headingDeg - 140, POSE_RADIUS * 0.72)
      ctx.beginPath()
      ctx.moveTo(nose.x, nose.y)
      ctx.lineTo(left.x, left.y)
      ctx.lineTo(right.x, right.y)
      ctx.closePath()
      ctx.stroke()
      ctx.fill()
    }
    ctx.fillStyle = palette.muted
    ctx.fillText(device.label, at.x + POSE_RADIUS + 3, at.y - POSE_RADIUS)
  }
}

/** Degrees counter-clockwise from +x in the world, on a y-down canvas. */
function offset(from: Point, headingDeg: number, distance: number): Point {
  const radians = (headingDeg * Math.PI) / 180
  return {
    x: from.x + Math.cos(radians) * distance,
    y: from.y - Math.sin(radians) * distance,
  }
}
