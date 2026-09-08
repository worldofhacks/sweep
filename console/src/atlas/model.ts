import * as THREE from 'three'

/** Fit the observed-area sphere into both axes, including a narrow tablet canvas. */
export function framingDistance(radius: number, aspect: number, verticalFov: number): number {
  const vertical = THREE.MathUtils.degToRad(verticalFov)
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * Math.max(.05, aspect))
  return radius * 1.1 / Math.sin(Math.min(vertical, horizontal) / 2)
}

/** GLTFLoader can resolve successfully even when a texture decode or CSP fetch failed. */
export function assertPhotoTextures(root: THREE.Object3D): void {
  let meshes = 0
  root.traverse(object => {
    if (!(object instanceof THREE.Mesh)) return
    meshes++
    const materials = Array.isArray(object.material) ? object.material : [object.material]
    for (const material of materials) {
      const texture = (material as THREE.MeshBasicMaterial).map
      if (!texture || !texture.image || !(texture.image.width > 0) || !(texture.image.height > 0)) {
        throw new Error('The photo textures could not be loaded. Reopen the 3D view to retry; source captures remain available.')
      }
    }
  })
  if (!meshes) throw new Error('The reconstructed surface is missing.')
}

/** Release both GPU resources and decoded image memory, including shared texture atlases. */
export function disposeModel(root: THREE.Object3D): void {
  const geometries = new Set<THREE.BufferGeometry>()
  const materials = new Set<THREE.Material>()
  const textures = new Set<THREE.Texture>()
  const images = new Set<ImageBitmap>()
  root.traverse(object => {
    if (!(object instanceof THREE.Points || object instanceof THREE.Mesh)) return
    geometries.add(object.geometry)
    ;(Array.isArray(object.material) ? object.material : [object.material]).forEach(material => materials.add(material))
  })
  materials.forEach(material => {
    Object.values(material).forEach(value => { if (value instanceof THREE.Texture) textures.add(value) })
    material.dispose()
  })
  textures.forEach(texture => {
    if (typeof ImageBitmap !== 'undefined' && texture.image instanceof ImageBitmap) images.add(texture.image)
    texture.dispose()
  })
  geometries.forEach(geometry => geometry.dispose())
  images.forEach(bitmap => bitmap.close())
}
