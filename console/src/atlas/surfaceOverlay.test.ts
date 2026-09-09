import { expect, test, vi } from 'vitest'
import * as THREE from 'three'
import { currentSurfaceRegion, surfaceOverlay } from './surfaceOverlay'
import { disposeModel } from './model'
import type { SurfaceFocus } from './types'

const focus: SurfaceFocus = { job_id: 'job', artifact_sha256: 'checksum', region: {
  id: 'region', label: 'Region 1', radius: 2, center: [1, 2, 3], boundary_edges: 3,
  segments: [[1, 2, 3], [1, 3, 4]],
} }

test('old build or checksum never places a review highlight on different geometry', () => {
  expect(currentSurfaceRegion(focus, 'other', 'checksum')).toBeNull()
  expect(currentSurfaceRegion(focus, 'job', 'other')).toBeNull()
  expect(currentSurfaceRegion(null, 'job', 'checksum')).toBeNull()
  expect(currentSurfaceRegion(focus, 'job', 'checksum')).toEqual(focus.region)
})

test('annotations reject invalid or unbounded points and radius', () => {
  for (const changes of [{ radius: 0 }, { radius: Infinity }, { center: [NaN, 1, 2] },
    { segments: [[1, 2, 3]] }, { segments: Array.from({ length: 258 }, () => [1, 2, 3]) }]) {
    expect(currentSurfaceRegion({ ...focus, region: { ...focus.region, ...changes } } as SurfaceFocus, 'job', 'checksum')).toBeNull()
  }
})

test('overlay uses only supplied edge segments, matches model orientation and releases all resources', () => {
  const lines = surfaceOverlay(focus.region)
  expect(lines).toBeInstanceOf(THREE.LineSegments)
  expect(Array.from(lines.geometry.getAttribute('position').array)).toEqual(focus.region.segments.flat())
  expect(lines.rotation.x).toBe(Math.PI)
  expect((lines.material as THREE.LineBasicMaterial).depthTest).toBe(false)
  const geometry = vi.spyOn(lines.geometry, 'dispose')
  const material = vi.spyOn(lines.material as THREE.LineBasicMaterial, 'dispose')
  disposeModel(lines)
  expect(geometry).toHaveBeenCalledTimes(1)
  expect(material).toHaveBeenCalledTimes(1)
})
