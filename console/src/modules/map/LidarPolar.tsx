import { useEffect, useRef } from 'react'
import './map.css'
import { formatDeviceId } from '../../control/state'
import type { RelayAircraftState, RelaySensorEvent } from '../../relay/contract'
import { prepareCanvas } from './canvas'
import { readMapPalette, scanColor } from './palette'
import { drawPolarScan, polarRange } from './polar'
import { scanReturns } from './projection'

export interface LidarPolarProps {
  device: Pick<RelayAircraftState, 'device_class' | 'unit'>
  /** The device's newest scan, or null when none has arrived this epoch. */
  scan: RelaySensorEvent | null
  /** Canvas edge in CSS pixels. */
  size?: number
}

const DEFAULT_SIZE = 96

/**
 * The newest scan for one device, plotted in its own frame with forward up.
 * Without a scan it says so rather than drawing an empty circle.
 */
export function LidarPolar({ device, scan, size = DEFAULT_SIZE }: LidarPolarProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const label = formatDeviceId(device)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !scan) return
    const context = prepareCanvas(canvas, size, size)
    if (!context) return
    const palette = readMapPalette(canvas)
    drawPolarScan(context, { size, palette, colour: scanColor(palette, device.unit), scan })
  }, [device.unit, scan, size])
  if (!scan) {
    return (
      <p className="mp-polar-empty" aria-label={`${label} lidar scan`}>
        No scan reported.
      </p>
    )
  }
  const returns = scanReturns(scan).length
  return (
    <figure className="mp-polar" aria-label={`${label} lidar scan`}>
      <canvas
        ref={canvasRef}
        className="mp-polar-canvas"
        style={{ width: `${size}px`, height: `${size}px` }}
        role="img"
        aria-label={`${label} polar lidar plot`}
      />
      <figcaption className="mp-polar-caption">
        {returns} of {scan.ranges_cm.length} returns · {polarRange(scan).toFixed(1)} m
      </figcaption>
    </figure>
  )
}
