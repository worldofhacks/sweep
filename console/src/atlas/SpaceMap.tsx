import { useEffect, useRef, useState } from 'react'
import maplibregl, { type GeoJSONSource, type Map as LibreMap } from 'maplibre-gl'
import type { Feature, FeatureCollection, Geometry } from 'geojson'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { GeoPosition, Space, SpaceDetail } from './types'

interface Props {
  spaces: Space[]
  detail: SpaceDetail | null
  center: [number, number]
  picking: boolean
  coverageVisible: boolean
  selectedCell: string | null
  position: GeoPosition | null
  onSelect: (id: string) => void
  onPick: (longitude: number, latitude: number) => void
  onCell: (id: string) => void
}
const collection = (features: Feature<Geometry>[]): FeatureCollection => ({
  type: 'FeatureCollection',
  features,
})
function ring(space: Space): number[][] {
  return Array.from({ length: 65 }, (_, i) => {
    const a = (i / 64) * Math.PI * 2
    return [
      space.longitude +
        (Math.cos(a) * space.radius) / (111320 * Math.cos((space.latitude * Math.PI) / 180)),
      space.latitude + (Math.sin(a) * space.radius) / 111320,
    ]
  })
}

export default function SpaceMap(props: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<LibreMap | null>(null)
  const callbacks = useRef(props)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    callbacks.current = props
  })
  useEffect(() => {
    if (!container.current) return
    let instance: LibreMap
    try {
      instance = new maplibregl.Map({
        container: container.current,
        center: callbacks.current.center,
        zoom: 14.5,
        minZoom: 0,
        maxZoom: 22,
        attributionControl: { compact: true },
        style: {
          version: 8,
          sources: {
            streets: {
              type: 'raster',
              tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
              tileSize: 256,
              // Reuse the last real street tile when inspecting a small area.
              // Zooming closer must never request nonexistent z20–22 tiles.
              maxzoom: 19,
              attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            },
          },
          layers: [
            {
              id: 'streets',
              type: 'raster',
              source: 'streets',
              paint: {
                'raster-saturation': -0.85,
                'raster-contrast': -0.1,
                'raster-opacity': 0.8,
              },
            },
          ],
        },
      })
    } catch {
      // A readable, functional directory stays present when WebGL is unavailable.
      queueMicrotask(() =>
        setError('The map needs WebGL. You can still browse and contribute using the space list.'),
      )
      return
    }
    map.current = instance
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    instance.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left')
    instance.on('load', () => {
      instance.addSource('areas', { type: 'geojson', data: collection([]) })
      instance.addLayer({
        id: 'area-fill',
        type: 'fill',
        source: 'areas',
        paint: { 'fill-color': '#236953', 'fill-opacity': 0.07 },
      })
      instance.addLayer({
        id: 'area-line',
        type: 'line',
        source: 'areas',
        paint: {
          'line-color': '#236953',
          'line-opacity': 0.65,
          'line-width': 1.5,
          'line-dasharray': [4, 3],
        },
      })
      instance.addSource('coverage', { type: 'geojson', data: collection([]) })
      instance.addLayer({
        id: 'coverage-fill',
        type: 'fill',
        source: 'coverage',
        paint: {
          'fill-color': [
            'case',
            ['==', ['get', 'selected'], true],
            '#dfb350',
            ['>', ['get', 'captures'], 0],
            '#328b69',
            '#939e9b',
          ],
          'fill-opacity': [
            'case',
            ['==', ['get', 'selected'], true],
            0.65,
            ['>', ['get', 'captures'], 0],
            0.4,
            0.16,
          ],
        },
      })
      instance.addLayer({
        id: 'coverage-line',
        type: 'line',
        source: 'coverage',
        paint: {
          'line-color': '#ffffff',
          'line-width': 1,
          'line-opacity': 0.7,
        },
      })
      instance.addSource('observations', {
        type: 'geojson',
        data: collection([]),
      })
      instance.addLayer({
        id: 'observation-points',
        type: 'circle',
        source: 'observations',
        paint: {
          'circle-radius': ['case', ['==', ['get', 'kind'], 'person'], 7, 4],
          'circle-color': ['case', ['==', ['get', 'kind'], 'person'], '#245ca6', '#21644f'],
          'circle-stroke-color': '#fff',
          'circle-stroke-width': 2,
        },
      })
      instance.on('click', (event) => {
        if (callbacks.current.picking) callbacks.current.onPick(event.lngLat.lng, event.lngLat.lat)
        else if (callbacks.current.coverageVisible) {
          const cell = instance.queryRenderedFeatures(event.point, {
            layers: ['coverage-fill'],
          })[0]
          if (cell?.properties?.id) callbacks.current.onCell(String(cell.properties.id))
        }
      })
      setReady(true)
    })
    instance.on('error', () =>
      setError('Some map tiles could not load. Space locations and captures remain available.'),
    )
    const resize = new ResizeObserver(() => {
      instance.resize()
      // Mercator's world is 512 logical pixels at z0. A taller viewport cannot
      // zoom that far out without showing empty poles. Match the control's bound
      // to that real limit (round up to avoid floating-point enabled/no-op clicks).
      const height = container.current?.clientHeight ?? 0
      instance.setMinZoom(Math.max(0, Math.ceil(Math.log2(Math.max(512, height) / 512) * 1e6) / 1e6))
    })
    resize.observe(container.current)
    return () => {
      resize.disconnect()
      instance.remove()
      map.current = null
    }
  }, [])

  const { spaces, detail, center, coverageVisible, selectedCell, position, picking } = props
  const longitude = center[0]
  const latitude = center[1]
  const detailId = detail?.space.id
  const hasPlace = spaces.length > 0 || Boolean(position)
  useEffect(() => {
    const instance = map.current
    if (!instance || !ready) return
    instance.easeTo({
      center: [longitude, latitude],
      zoom: detailId || picking ? 17.5 : 14.5,
      duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 650,
    })
  }, [longitude, latitude, detailId, ready, picking, hasPlace])

  useEffect(() => {
    const instance = map.current
    if (!instance || !ready) return
    const shown = detail ? [detail.space] : spaces
    ;(instance.getSource('areas') as GeoJSONSource).setData(
      collection(
        shown.map((space) => ({
          type: 'Feature',
          properties: {},
          geometry: { type: 'Polygon', coordinates: [ring(space)] },
        })),
      ),
    )
    const markers = shown.map((space) => {
      const element = document.createElement('button')
      element.className = `atlas-marker ${space.category} ${detail ? 'is-selected' : ''}`
      element.setAttribute('aria-label', `Open ${space.title}`)
      element.textContent = space.category === 'incident' || space.category === 'hazard' ? '!' : '+'
      element.onclick = (event) => {
        event.stopPropagation()
        callbacks.current.onSelect(space.id)
      }
      return new maplibregl.Marker({ element })
        .setLngLat([space.longitude, space.latitude])
        .addTo(instance)
    })
    return () => markers.forEach((marker) => marker.remove())
  }, [spaces, detail, ready])

  useEffect(() => {
    const instance = map.current
    if (!instance || !ready) return
    ;(instance.getSource('coverage') as GeoJSONSource).setData(
      collection(
        coverageVisible && detail
          ? detail.coverage.cells.map((cell) => {
              const dx = cell.size / 2 / (111320 * Math.cos((cell.latitude * Math.PI) / 180)),
                dy = cell.size / 2 / 111320
              return {
                type: 'Feature',
                properties: {
                  id: cell.id,
                  captures: cell.captures,
                  selected: cell.id === selectedCell,
                },
                geometry: {
                  type: 'Polygon',
                  coordinates: [
                    [
                      [cell.longitude - dx, cell.latitude - dy],
                      [cell.longitude + dx, cell.latitude - dy],
                      [cell.longitude + dx, cell.latitude + dy],
                      [cell.longitude - dx, cell.latitude + dy],
                      [cell.longitude - dx, cell.latitude - dy],
                    ],
                  ],
                },
              }
            })
          : [],
      ),
    )
    const points: Feature<Geometry>[] = []
    detail?.captures.forEach((c) => {
      if (c.position)
        points.push({
          type: 'Feature',
          properties: { kind: 'capture' },
          geometry: {
            type: 'Point',
            coordinates: [c.position.longitude, c.position.latitude],
          },
        })
    })
    detail?.people.forEach((p) =>
      points.push({
        type: 'Feature',
        properties: { kind: 'person' },
        geometry: {
          type: 'Point',
          coordinates: [p.position.longitude, p.position.latitude],
        },
      }),
    )
    if (position)
      points.push({
        type: 'Feature',
        properties: { kind: 'person' },
        geometry: {
          type: 'Point',
          coordinates: [position.longitude, position.latitude],
        },
      })
    ;(instance.getSource('observations') as GeoJSONSource).setData(collection(points))
  }, [detail, ready, coverageVisible, selectedCell, position])

  return (
    <div className={`atlas-map ${picking ? 'is-picking' : ''}`}>
      <div ref={container} className="atlas-map-canvas" aria-label="Geographic map of spaces" aria-busy={!ready && !error} />
      <button className="atlas-map-recenter atlas-secondary" disabled={!ready}
        onClick={() => map.current?.easeTo({ center: props.center, zoom: detailId || picking ? 17.5 : 14.5,
          duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 650 })}>
        {detailId ? 'Recenter area' : 'Recenter map'}
      </button>
      {picking && (
        <div className="atlas-map-crosshair" aria-hidden="true">
          +
        </div>
      )}
      {!ready && !error && <p className="atlas-map-error" role="status">Loading street map…</p>}
      {error && (
        <p className="atlas-map-error" role="status">
          {error}
        </p>
      )}
    </div>
  )
}
