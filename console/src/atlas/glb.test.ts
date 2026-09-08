import { describe, expect, test } from 'vitest'
import { validateAtlasGlb } from './glb'

/** Transport fixtures only; repeated triangles are not reconstruction-quality evidence. */
function meshFixture() {
  const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF9sAAAAASUVORK5CYII='), c => c.charCodeAt(0))
  const binary = new Uint8Array(Math.ceil((660 + png.length) / 4) * 4)
  new Float32Array(binary.buffer, 0, 9).set([0, 0, 0, 1, 0, 0, 0, 1, 0])
  new Uint32Array(binary.buffer, 36, 150).set(Array.from({ length: 150 }, (_, i) => i % 3))
  new Float32Array(binary.buffer, 636, 6).set([0, 0, 1, 0, 0, 1])
  binary.set(png, 660)
  const document = {
    asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, TEXCOORD_0: 2 }, indices: 1, material: 0, mode: 4 }] }],
    buffers: [{ byteLength: binary.length }],
    bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: 36 },
      { buffer: 0, byteOffset: 36, byteLength: 600 }, { buffer: 0, byteOffset: 636, byteLength: 24 },
      { buffer: 0, byteOffset: 660, byteLength: png.length }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3' },
      { bufferView: 1, componentType: 5125, count: 150, type: 'SCALAR' },
      { bufferView: 2, componentType: 5126, count: 3, type: 'VEC2' }],
    images: [{ bufferView: 3, mimeType: 'image/png' }], textures: [{ source: 0 }],
    materials: [{ extensions: { KHR_materials_unlit: {} }, pbrMetallicRoughness: { baseColorTexture: { index: 0 } } }],
    extensionsUsed: ['KHR_materials_unlit'],
  }
  return { document, binary }
}

function glb(document: object, binary: Uint8Array): ArrayBuffer {
  const json = new TextEncoder().encode(JSON.stringify(document))
  const jsonSize = Math.ceil(json.length / 4) * 4
  const data = new ArrayBuffer(28 + jsonSize + binary.length)
  const header = new DataView(data)
  header.setUint32(0, 0x46546c67, true); header.setUint32(4, 2, true); header.setUint32(8, data.byteLength, true)
  header.setUint32(12, jsonSize, true); header.setUint32(16, 0x4e4f534a, true)
  new Uint8Array(data, 20, jsonSize).fill(32)
  new Uint8Array(data, 20, json.length).set(json)
  header.setUint32(20 + jsonSize, binary.length, true); header.setUint32(24 + jsonSize, 0x004e4942, true)
  new Uint8Array(data, 28 + jsonSize).set(binary)
  return data
}

describe('bounded Atlas GLB profiles', () => {
  test('accepts embedded photo-textured triangles without requesting external resources', () => {
    const { document, binary } = meshFixture()
    expect(validateAtlasGlb(glb(document, binary))).toEqual({ representation: 'textured_mesh', vertices: 3, faces: 50 })
  })

  test('retains compatibility with the existing colored sparse-points output', () => {
    const binary = new Uint8Array(48)
    new Float32Array(binary.buffer, 0, 9).set([0, 0, 0, 1, 0, 0, 0, 1, 0])
    binary.fill(255, 36)
    const document = {
      asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
      meshes: [{ primitives: [{ attributes: { POSITION: 0, COLOR_0: 1 }, mode: 0 }] }],
      buffers: [{ byteLength: 48 }], bufferViews: [{ buffer: 0, byteLength: 36 }, { buffer: 0, byteOffset: 36, byteLength: 12 }],
      accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3' },
        { bufferView: 1, componentType: 5121, count: 3, type: 'VEC4', normalized: true }],
    }
    expect(validateAtlasGlb(glb(document, binary))).toEqual({ representation: 'sparse_point_cloud', vertices: 3, faces: 0 })
  })

  test.each(['uri', 'extension', 'count', 'indices', 'nan', 'texture-size', 'texture-source', 'material', 'scene', 'stride', 'version', 'length'])(
    'rejects an invalid %s before handing anything to the renderer', fault => {
      const { document, binary } = meshFixture()
      if (fault === 'uri') Object.assign(document.images[0], { uri: 'https://example.test/texture.png' })
      if (fault === 'extension') Object.assign(document.textures[0], { extensions: { EXT_texture_webp: { source: 0 } } })
      if (fault === 'count') document.accessors[0].count = 1000000000
      if (fault === 'indices') new Uint32Array(binary.buffer, 36, 150)[0] = 3
      if (fault === 'nan') new Float32Array(binary.buffer, 0, 9)[0] = NaN
      if (fault === 'texture-size') new DataView(binary.buffer).setUint32(676, 8192)
      if (fault === 'texture-source') document.textures[0].source = 2
      if (fault === 'material') document.materials[0].pbrMetallicRoughness.baseColorTexture.index = 2
      if (fault === 'scene') Object.assign(document.nodes[0], { children: [0] })
      if (fault === 'stride') Object.assign(document.bufferViews[0], { byteStride: 20 })
      const data = glb(document, binary)
      if (fault === 'version') new DataView(data).setUint32(4, 1, true)
      if (fault === 'length') new DataView(data).setUint32(12, data.byteLength, true)
      expect(() => validateAtlasGlb(data)).toThrow()
    },
  )
})
