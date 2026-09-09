import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SpaceMap from './SpaceMap'
import type { Space } from './types'

const mock = vi.hoisted(() => ({
  options: {} as Record<string, unknown>,
  handlers: {} as Record<string, (event?: unknown) => void>,
  easeTo: vi.fn(), remove: vi.fn(), resize: vi.fn(), setData: vi.fn(), setMinZoom: vi.fn(), setTiles: vi.fn(),
  tilesLoaded: false,
}))
vi.mock('maplibre-gl', () => ({ default: {
  Map: class {
    constructor(options: Record<string, unknown>) { mock.options = options }
    on(name: string, callback: (event?: unknown) => void) { mock.handlers[name] = callback }
    addControl() {} addSource() {} addLayer() {}
    getSource() { return { setData: mock.setData, setTiles: mock.setTiles } }
    easeTo = mock.easeTo
    remove = mock.remove
    resize = mock.resize
    setMinZoom = mock.setMinZoom
    areTilesLoaded() { return mock.tilesLoaded }
  },
  NavigationControl: class {}, ScaleControl: class {},
  Marker: class { setLngLat() { return this } addTo() { return this } remove() {} },
} }))
beforeEach(() => {
  vi.clearAllMocks()
  mock.handlers = {}
  mock.tilesLoaded = false
  vi.stubGlobal('ResizeObserver', class {
    callback: () => void
    constructor(callback: () => void) { this.callback = callback }
    observe() { this.callback() }
    disconnect() {}
  })
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })
const space: Space = { id: 'demo', title: 'Austin demo', category: 'community',
  latitude: 30.2672, longitude: -97.7431, radius: 100, place: 'Austin, Texas', description: 'Demo only',
  status: 'active', verification: 'unverified', created_at: 1, updated_at: 1,
  capture_count: 0, coverage_percent: 0, contributors: 0 }
const props = () => ({ spaces: [space], detail: null, center: [-97.7431, 30.2672] as [number, number],
  picking: false, coverageVisible: false, selectedCell: null, position: null,
  onSelect: vi.fn(), onPick: vi.fn(), onCell: vi.fn() })

it('keeps real street tiles across the complete map zoom range, including close inspection', () => {
  render(<SpaceMap {...props()} />)
  expect(screen.getByRole('status')).toHaveTextContent('Loading street map…')
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'true')
  expect(mock.options).toMatchObject({ minZoom: 0, maxZoom: 22,
    style: { sources: { streets: { maxzoom: 19, tileSize: 256 } },
      layers: [expect.objectContaining({ id: 'streets', type: 'raster' })] } })
  expect(screen.getByRole('button', { name: 'Recenter map' })).toBeDisabled()
  act(() => mock.handlers.load())
  expect(screen.queryByText('Loading street map…')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'false')
  expect(screen.getByRole('button', { name: 'Recenter map' })).toBeEnabled()
})

it('starts place selection at city scale and its controls never submit an enclosing form', () => {
  const submit = vi.fn(event => event.preventDefault())
  render(<form onSubmit={submit}><SpaceMap {...props()} spaces={[]} picking pickingZoom={11} overviewZoom={11} /></form>)
  act(() => mock.handlers.load())
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 11 }))
  fireEvent.click(screen.getByRole('button', { name: 'Recenter map' }))
  expect(submit).not.toHaveBeenCalled()
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 11 }))
})

it('reports an unavailable basemap without leaving the loading state indefinitely', () => {
  render(<SpaceMap {...props()} />)
  act(() => mock.handlers.error())
  expect(screen.getByRole('status')).toHaveTextContent('Some map tiles could not load.')
  expect(screen.queryByText('Loading street map…')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'false')
})

it('retries only street tiles without changing the selected map view or overlays', () => {
  render(<SpaceMap {...props()} />)
  act(() => mock.handlers.load())
  act(() => mock.handlers.error())
  mock.easeTo.mockClear()
  mock.setData.mockClear()
  fireEvent.click(screen.getByRole('button', { name: 'Retry map' }))
  expect(mock.setTiles).toHaveBeenCalledExactlyOnceWith(['https://tile.openstreetmap.org/{z}/{x}/{y}.png'])
  expect(mock.easeTo).not.toHaveBeenCalled()
  expect(mock.setData).not.toHaveBeenCalled()
  expect(mock.remove).not.toHaveBeenCalled()
  expect(screen.getByRole('status')).toHaveTextContent('Retrying street map…')
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'true')
  act(() => mock.handlers.idle())
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'false')
})

it('retains a repeated tile failure even when the map becomes idle', () => {
  render(<SpaceMap {...props()} />)
  act(() => mock.handlers.error())
  fireEvent.click(screen.getByRole('button', { name: 'Retry map' }))
  act(() => { mock.handlers.error(); mock.handlers.idle() })
  expect(screen.getByRole('status')).toHaveTextContent('Some map tiles could not load.')
  expect(screen.getByRole('button', { name: 'Retry map' })).toBeEnabled()
  expect(screen.getByLabelText('Geographic map of spaces')).toHaveAttribute('aria-busy', 'false')
})

it('does not reset a manually chosen zoom during space polling, but provides explicit recentering', () => {
  const value = props()
  const view = render(<SpaceMap {...value} />)
  act(() => mock.handlers.load())
  const calls = mock.easeTo.mock.calls.length
  view.rerender(<SpaceMap {...value} center={[...value.center]} spaces={[{ ...space, updated_at: 2 }]} />)
  expect(mock.easeTo).toHaveBeenCalledTimes(calls)
  fireEvent.click(screen.getByRole('button', { name: 'Recenter map' }))
  expect(mock.easeTo).toHaveBeenLastCalledWith({ center: value.center, zoom: 14.5, duration: 0 })
  view.unmount()
  expect(mock.remove).toHaveBeenCalledTimes(1)
})

it('opens and recenters editorial discovery at city scale, retaining close survey inspection', () => {
  const view = render(<SpaceMap {...props()} overviewZoom={12} />)
  expect(mock.options.zoom).toBe(12)
  act(() => mock.handlers.load())
  fireEvent.click(screen.getByRole('button', { name: 'Recenter map' }))
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 12 }))
  view.rerender(<SpaceMap {...props()} picking overviewZoom={12} />)
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 17.5 }))
})

it('fits city examples into the shorter phone map without changing ordinary discovery zoom', () => {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(390)
  const view = render(<SpaceMap {...props()} overviewZoom={12} />)
  expect(mock.options.zoom).toBe(11)
  act(() => mock.handlers.load())
  fireEvent.click(screen.getByRole('button', { name: 'Recenter map' }))
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 11 }))
  view.rerender(<SpaceMap {...props()} />)
  expect(mock.easeTo).toHaveBeenLastCalledWith(expect.objectContaining({ zoom: 14.5 }))
})

it.each(['community', 'survey', 'hazard'] as const)('retains the same basemap for %s spaces', category => {
  render(<SpaceMap {...props()} spaces={[{ ...space, category }]} />)
  act(() => mock.handlers.load())
  expect(screen.getByLabelText('Geographic map of spaces')).toBeInTheDocument()
  expect(mock.setData).toHaveBeenCalledWith(expect.objectContaining({ type: 'FeatureCollection' }))
  expect(mock.options.maxZoom).toBe(22)
})

it.each([[250, 0], [512, 0], [1024, 1], [827, 0.69175]])('bounds zoom-out to a %ipx viewport without empty poles', (height, minimum) => {
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(height)
  render(<SpaceMap {...props()} />)
  expect(mock.setMinZoom.mock.calls[0][0]).toBeCloseTo(minimum, 3)
})
