import { afterEach, expect, test, vi } from 'vitest'
import * as THREE from 'three'
import { assertPhotoTextures, disposeModel, framingDistance } from './model'
import nativeHtml from '../../atlas-native.html?raw'

afterEach(() => vi.unstubAllGlobals())

test.each([.35, .5, 1, 2])('frames the observed area along both axes at aspect %s', aspect => {
  const vertical = THREE.MathUtils.degToRad(50)
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * aspect)
  const distance = framingDistance(3, aspect, 50)
  expect(distance * Math.sin(vertical / 2)).toBeGreaterThanOrEqual(3.3 - 1e-9)
  expect(distance * Math.sin(horizontal / 2)).toBeGreaterThanOrEqual(3.3 - 1e-9)
})

test('missing or failed texture decoding is an error even when the loader returns a scene', () => {
  const scene = new THREE.Scene()
  expect(() => assertPhotoTextures(scene)).toThrow('surface is missing')
  const material = new THREE.MeshBasicMaterial()
  scene.add(new THREE.Mesh(new THREE.BufferGeometry(), material))
  expect(() => assertPhotoTextures(scene)).toThrow('photo textures could not be loaded')
  material.map = new THREE.Texture(document.createElement('img'))
  expect(() => assertPhotoTextures(scene)).toThrow('photo textures could not be loaded')
  material.map.image = { width: 2048, height: 2048 }
  expect(() => assertPhotoTextures(scene)).not.toThrow()
  disposeModel(scene)
})

test('page changes can release shared geometry, materials, textures, and bitmaps exactly once', () => {
  class Bitmap { width = 2048; height = 2048; close = vi.fn() }
  vi.stubGlobal('ImageBitmap', Bitmap)
  const bitmap = new Bitmap()
  const texture = new THREE.Texture(bitmap)
  const material = new THREE.MeshBasicMaterial({ map: texture })
  const geometry = new THREE.BufferGeometry()
  const scene = new THREE.Scene()
  scene.add(new THREE.Mesh(geometry, material), new THREE.Mesh(geometry, material))
  const dispose = [geometry, material, texture].map(resource => vi.spyOn(resource, 'dispose'))
  assertPhotoTextures(scene)
  disposeModel(scene)
  dispose.forEach(spy => expect(spy).toHaveBeenCalledTimes(1))
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})

test('Android permits embedded texture blob reads without opening arbitrary network or script access', () => {
  const html = new DOMParser().parseFromString(nativeHtml, 'text/html')
  const policy = html.querySelector('meta[http-equiv="Content-Security-Policy"]')!.getAttribute('content')!
  const directives = Object.fromEntries(policy.split(';').map(value => {
    const [name, ...sources] = value.trim().split(/\s+/)
    return [name, sources]
  }))
  expect(directives['connect-src']).toEqual(["'self'", 'blob:', 'https://tile.openstreetmap.org'])
  expect(directives['script-src']).toEqual(["'self'"])
  expect(directives['default-src']).toEqual(["'none'"])
})
