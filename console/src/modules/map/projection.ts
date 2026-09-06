import type { RelaySensorEvent, SweepBox } from '../../relay/contract'

/**
 * The map is drawn in the room frame: x east, y north, metres. The canvas is
 * y-down, so every projection flips y exactly once, here.
 */
export interface MapView {
  /** World coordinates at the centre of the canvas. */
  centerX: number
  centerY: number
  /** Canvas pixels per metre. */
  scale: number
}

export interface Viewport {
  width: number
  height: number
}

export interface Point {
  x: number
  y: number
}

export const MIN_SCALE = 4
export const MAX_SCALE = 400
export const DEFAULT_VIEW: MapView = { centerX: 0, centerY: 0, scale: 48 }

export function worldToCanvas(view: MapView, viewport: Viewport, x: number, y: number): Point {
  return {
    x: viewport.width / 2 + (x - view.centerX) * view.scale,
    y: viewport.height / 2 - (y - view.centerY) * view.scale,
  }
}

export function canvasToWorld(view: MapView, viewport: Viewport, px: number, py: number): Point {
  return {
    x: view.centerX + (px - viewport.width / 2) / view.scale,
    y: view.centerY - (py - viewport.height / 2) / view.scale,
  }
}

/** A drag of the canvas moves the world under it by the same pixels. */
export function panView(view: MapView, dxPixels: number, dyPixels: number): MapView {
  return {
    ...view,
    centerX: view.centerX - dxPixels / view.scale,
    centerY: view.centerY + dyPixels / view.scale,
  }
}

/** Zooms about a canvas point so the world under the pointer stays under it. */
export function zoomView(
  view: MapView,
  viewport: Viewport,
  factor: number,
  px: number,
  py: number,
): MapView {
  const scale = clampScale(view.scale * factor)
  if (scale === view.scale) return view
  const anchor = canvasToWorld(view, viewport, px, py)
  const next = { ...view, scale }
  const after = canvasToWorld(next, viewport, px, py)
  return { scale, centerX: view.centerX + anchor.x - after.x, centerY: view.centerY + anchor.y - after.y }
}

export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return DEFAULT_VIEW.scale
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale))
}

/** Centres a world box in the viewport with a margin, for the fit control. */
export function fitView(box: SweepBox, viewport: Viewport, marginPixels = 24): MapView {
  const width = Math.max(box.max_x - box.min_x, 0.1)
  const height = Math.max(box.max_y - box.min_y, 0.1)
  const usableWidth = Math.max(viewport.width - marginPixels * 2, 1)
  const usableHeight = Math.max(viewport.height - marginPixels * 2, 1)
  return {
    centerX: (box.min_x + box.max_x) / 2,
    centerY: (box.min_y + box.max_y) / 2,
    scale: clampScale(Math.min(usableWidth / width, usableHeight / height)),
  }
}

/**
 * The scan's returns as world points. Angle 0 points along the device's
 * forward axis and angles increase counter-clockwise (contract section 4), so
 * a bin's world bearing is the pose yaw plus the bin angle. A zero range is no
 * return; a range outside [range_min_m, range_max_m] is dropped rather than
 * drawn where the sensor did not see.
 */
export function scanPoints(scan: RelaySensorEvent): Point[] {
  const points: Point[] = []
  for (const { angleDeg, rangeM } of scanReturns(scan)) {
    const bearing = ((scan.pose.yaw_deg + angleDeg) * Math.PI) / 180
    points.push({
      x: scan.pose.x + rangeM * Math.cos(bearing),
      y: scan.pose.y + rangeM * Math.sin(bearing),
    })
  }
  return points
}

/** The same returns in the device frame, for the polar plot. */
export function scanReturns(scan: RelaySensorEvent): Array<{ angleDeg: number; rangeM: number }> {
  const returns: Array<{ angleDeg: number; rangeM: number }> = []
  scan.ranges_cm.forEach((centimetres, bin) => {
    if (centimetres <= 0) return
    const rangeM = centimetres / 100
    if (rangeM < scan.range_min_m || rangeM > scan.range_max_m) return
    returns.push({ angleDeg: scan.angle_min_deg + bin * scan.angle_increment_deg, rangeM })
  })
  return returns
}
