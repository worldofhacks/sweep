import * as THREE from 'three'
import type { SurfaceFocus, SurfaceRegion } from './types'

export function currentSurfaceRegion(focus: SurfaceFocus | null | undefined, jobId: string, checksum: string): SurfaceRegion | null {
  if (!focus || focus.job_id !== jobId || focus.artifact_sha256 !== checksum) return null
  const region = focus.region
  const point = (value: number[]) => value.length === 3 && value.every(Number.isFinite)
  if (!point(region.center) || !Number.isFinite(region.radius) || region.radius <= 0
    || region.segments.length < 2 || region.segments.length > 256 || region.segments.length % 2
    || !region.segments.every(point)) return null
  return region
}

/** Annotation only. Does not add or fill any surface in the reconstructed model. */
export function surfaceOverlay(region: SurfaceRegion): THREE.LineSegments {
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(region.segments.flat(), 3))
  const lines = new THREE.LineSegments(geometry,
    new THREE.LineBasicMaterial({ color: '#d8f3c7', depthTest: false, depthWrite: false }))
  lines.rotation.x = Math.PI // Same COLMAP-local display transform as the reconstructed scene.
  lines.renderOrder = 1
  return lines
}
