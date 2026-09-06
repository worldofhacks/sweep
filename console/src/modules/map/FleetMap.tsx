import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './map.css'
import { formatDeviceId } from '../../control/state'
import { useSensorStore } from '../../sensor/store'
import { sortedAircraft } from '../../shell/derive'
import { formatTime } from '../../shell/format'
import type { ModuleProps } from '../types'
import { prepareCanvas } from './canvas'
import { scanningDevices, mapDevices, scanTrail } from './derive-map'
import { drawFleetMap, type MapScan } from './draw'
import { fetchOccupancyMap, resetOccupancyMap, type OccupancyMap } from './occupancy'
import { readMapPalette, scanColorToken } from './palette'
import {
  DEFAULT_VIEW,
  fitView,
  panView,
  zoomView,
  type MapView,
  type Viewport,
} from './projection'

/** The relay updates the grid on every accepted scan; the console reads it at 1 Hz. */
export const MAP_POLL_MS = 1_000

/** Used until the layout measures the canvas, and in environments without a layout. */
const DEFAULT_VIEWPORT: Viewport = { width: 640, height: 420 }

type MapStatus = 'idle' | 'map' | 'absent' | 'error'

/**
 * The fleet map: the relay's occupancy raster under the geofence, the scan
 * trails, each device's newest returns in its own colour, and every device
 * that reports a position, drawn as a heading triangle. Nothing here is
 * estimated between frames; a device without a position is listed, not placed.
 */
export function FleetMap({ controller, catalog, mapEndpoint }: ModuleProps) {
  const { state, sensors } = controller
  const snapshot = useSensorStore(sensors)
  const fleet = useMemo(() => sortedAircraft(state.aircraft), [state.aircraft])
  const devices = useMemo(() => mapDevices(fleet, snapshot), [fleet, snapshot])
  const scanned = useMemo(
    () =>
      scanningDevices(fleet, snapshot).map(({ device, scan }) => ({
        device,
        scan,
        trail: scanTrail(device, snapshot),
      })),
    [fleet, snapshot],
  )
  const scans = useMemo<MapScan[]>(
    () => scanned.map(({ device, scan, trail }) => ({ unit: device.unit, scan, trail })),
    [scanned],
  )
  const geofence = catalog.snapshot.config?.geofence ?? null

  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [viewport, setViewport] = useState<Viewport>(DEFAULT_VIEWPORT)
  const viewportRef = useRef(viewport)
  const [view, setView] = useState<MapView>(DEFAULT_VIEW)
  const [map, setMap] = useState<OccupancyMap | null>(null)
  const [status, setStatus] = useState<MapStatus>('idle')
  const [notice, setNotice] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)
  const drag = useRef<{ pointerId: number; x: number; y: number } | null>(null)

  // The wheel and button handlers are bound once, so they read the measured
  // viewport through a ref rather than a stale closure.
  useEffect(() => {
    viewportRef.current = viewport
  }, [viewport])

  // One read on mount and one a second after that, and none once unmounted:
  // the raster is only worth reading while somebody is looking at it.
  useEffect(() => {
    if (!mapEndpoint) return
    let cancelled = false
    const abort = new AbortController()
    const read = async () => {
      const result = await fetchOccupancyMap(mapEndpoint, { signal: abort.signal })
      if (cancelled) return
      setStatus(result.status)
      setMap(result.status === 'map' ? result.map : null)
    }
    void read()
    const timer = setInterval(() => void read(), MAP_POLL_MS)
    return () => {
      cancelled = true
      abort.abort()
      clearInterval(timer)
    }
  }, [mapEndpoint])

  useEffect(() => {
    const element = wrapRef.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      const width = element.clientWidth || DEFAULT_VIEWPORT.width
      const height = element.clientHeight || DEFAULT_VIEWPORT.height
      setViewport((previous) =>
        previous.width === width && previous.height === height ? previous : { width, height },
      )
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const context = prepareCanvas(canvas, viewport.width, viewport.height)
    if (!context) return
    drawFleetMap(context, {
      viewport,
      view,
      palette: readMapPalette(canvas),
      map: mapEndpoint ? map : null,
      geofence,
      devices,
      scans,
    })
  }, [devices, geofence, map, mapEndpoint, scans, view, viewport])

  // Wheel zoom has to be a non-passive listener to keep the pane from
  // scrolling under the pointer, which React's own wheel handler cannot be.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const bounds = canvas.getBoundingClientRect()
      const factor = Math.exp(-event.deltaY / 400)
      setView((previous) =>
        zoomView(
          previous,
          viewportRef.current,
          factor,
          event.clientX - bounds.left,
          event.clientY - bounds.top,
        ),
      )
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [])

  const zoomBy = useCallback((factor: number) => {
    const { width, height } = viewportRef.current
    setView((previous) => zoomView(previous, viewportRef.current, factor, width / 2, height / 2))
  }, [])

  const fit = useCallback(() => {
    const box = geofence ?? boundsOf(devices) ?? null
    setView(box ? fitView(box, viewportRef.current) : DEFAULT_VIEW)
  }, [devices, geofence])

  const resetMap = useCallback(async () => {
    if (!mapEndpoint) return
    setResetting(true)
    const cleared = await resetOccupancyMap(mapEndpoint)
    setResetting(false)
    setNotice(
      cleared
        ? 'The relay cleared the occupancy grid. The next read shows the grid rebuilding from new scans.'
        : 'The relay did not clear the occupancy grid.',
    )
    if (cleared) setMap(null)
  }, [mapEndpoint])

  const placed = new Set(devices.map((device) => device.droneId))
  const unplaced = fleet.filter((device) => !placed.has(device.drone_id))
  return (
    <div className="mp">
      <div className="mp-bar">
        <p className="mp-status" role="status">
          {statusSentence(status, map, Boolean(mapEndpoint))}
        </p>
        <div className="mp-actions">
          <button type="button" className="mp-button" onClick={() => zoomBy(1 / 1.4)}>
            Zoom out
          </button>
          <button type="button" className="mp-button" onClick={() => zoomBy(1.4)}>
            Zoom in
          </button>
          <button type="button" className="mp-button" onClick={fit}>
            Fit view
          </button>
          <button
            type="button"
            className="mp-button is-reset"
            disabled={!mapEndpoint || resetting}
            title={mapEndpoint ? undefined : 'This console was given no relay bootstrap.'}
            onClick={() => void resetMap()}
          >
            {resetting ? 'Resetting…' : 'Reset map'}
          </button>
        </div>
      </div>
      <div className="mp-canvas-wrap" ref={wrapRef}>
        <canvas
          ref={canvasRef}
          className="mp-canvas"
          role="img"
          aria-label="Fleet map"
          onPointerDown={(event) => {
            drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY }
            // Absent outside a browser with pointer capture; the drag still works.
            event.currentTarget.setPointerCapture?.(event.pointerId)
          }}
          onPointerMove={(event) => {
            const active = drag.current
            if (!active || active.pointerId !== event.pointerId) return
            const dx = event.clientX - active.x
            const dy = event.clientY - active.y
            drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY }
            setView((previous) => panView(previous, dx, dy))
          }}
          onPointerUp={(event) => {
            drag.current = null
            event.currentTarget.releasePointerCapture?.(event.pointerId)
          }}
          onPointerCancel={() => {
            drag.current = null
          }}
        />
      </div>
      <p className="mp-hint">
        Drag to pan, scroll to zoom. {view.scale.toFixed(0)} pixels per metre. The room frame is x
        east, y north, in metres.
      </p>
      {scanned.length > 0 && (
        <ul className="mp-legend" aria-label="Scanning devices">
          {scanned.map(({ device, scan }) => (
            <li key={device.drone_id}>
              <span
                className="mp-swatch"
                aria-hidden="true"
                style={{ background: `var(${scanColorToken(device.unit)})` }}
              />
              {`${formatDeviceId(device)} · ${scan.ranges_cm.length} bins · scanned ${formatTime(scan.t)}`}
            </li>
          ))}
        </ul>
      )}
      {unplaced.length > 0 && (
        <p className="mp-unplaced">
          No position reported for {unplaced.map(formatDeviceId).join(', ')}; they are not drawn.
        </p>
      )}
      {notice && (
        <p className="mp-notice" role="status">
          {notice}
        </p>
      )}
    </div>
  )
}

function boundsOf(devices: ReadonlyArray<{ x: number; y: number }>) {
  if (devices.length === 0) return null
  const xs = devices.map((device) => device.x)
  const ys = devices.map((device) => device.y)
  const margin = 2
  return {
    min_x: Math.min(...xs) - margin,
    max_x: Math.max(...xs) + margin,
    min_y: Math.min(...ys) - margin,
    max_y: Math.max(...ys) + margin,
  }
}

function statusSentence(status: MapStatus, map: OccupancyMap | null, configured: boolean): string {
  if (!configured) {
    return 'No relay bootstrap, so no occupancy map can be read. Poses and scans come from relay state and sensor frames.'
  }
  if (status === 'map' && map) {
    return `Occupancy map ${map.width}×${map.height} cells at ${map.resolution_m} m, updated ${formatTime(map.updated_at)}.`
  }
  if (status === 'absent') return 'The relay reports no occupancy map for this session yet.'
  if (status === 'error') return 'The occupancy map could not be read from the relay.'
  return 'Reading the occupancy map from the relay.'
}
