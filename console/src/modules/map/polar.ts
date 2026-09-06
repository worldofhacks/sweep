import type { RelaySensorEvent } from '../../relay/contract'
import type { MapPalette } from './palette'
import { scanReturns, type Point } from './projection'

/** Rings drawn at these fractions of the plotted range. */
export const POLAR_RINGS = [0.25, 0.5, 0.75, 1]
const EDGE = 6
const DOT = 2

export interface PolarScene {
  /** The square canvas edge, in CSS pixels. */
  size: number
  palette: MapPalette
  colour: string
  scan: RelaySensorEvent
}

/**
 * The plotted range: the farthest return, never beyond the sensor's maximum
 * and never below a metre, so a scan against a near wall still fills the plot
 * and the ring labels stay honest.
 */
export function polarRange(scan: RelaySensorEvent): number {
  const farthest = scanReturns(scan).reduce((max, item) => Math.max(max, item.rangeM), 0)
  return Math.min(scan.range_max_m, Math.max(1, farthest))
}

/**
 * Device frame with forward up: angle 0 points at the top of the plot and
 * angles increase counter-clockwise, matching the frame's convention.
 */
export function polarPoint(size: number, angleDeg: number, rangeM: number, rangeMax: number): Point {
  const centre = size / 2
  const radius = Math.max(centre - EDGE, 1)
  const fraction = rangeMax > 0 ? Math.min(rangeM / rangeMax, 1) : 0
  const radians = (angleDeg * Math.PI) / 180
  return {
    x: centre - Math.sin(radians) * radius * fraction,
    y: centre - Math.cos(radians) * radius * fraction,
  }
}

/** Range rings, the forward axis, then one dot per return. */
export function drawPolarScan(ctx: CanvasRenderingContext2D, scene: PolarScene): void {
  const { size, palette, colour, scan } = scene
  const centre = size / 2
  const radius = Math.max(centre - EDGE, 1)
  ctx.clearRect(0, 0, size, size)
  ctx.fillStyle = palette.ground
  ctx.fillRect(0, 0, size, size)
  ctx.strokeStyle = palette.frame
  ctx.lineWidth = 1
  for (const ring of POLAR_RINGS) {
    ctx.beginPath()
    ctx.arc(centre, centre, radius * ring, 0, Math.PI * 2)
    ctx.stroke()
  }
  ctx.beginPath()
  ctx.moveTo(centre, centre)
  ctx.lineTo(centre, centre - radius)
  ctx.stroke()
  const rangeMax = polarRange(scan)
  ctx.fillStyle = colour
  for (const { angleDeg, rangeM } of scanReturns(scan)) {
    const at = polarPoint(size, angleDeg, rangeM, rangeMax)
    ctx.fillRect(at.x - DOT / 2, at.y - DOT / 2, DOT, DOT)
  }
}
