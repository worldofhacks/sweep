/** The two bounded static profiles the Atlas worker publishes, checked before GLTFLoader. */
export interface AtlasGeometry {
  representation: 'sparse_point_cloud' | 'textured_mesh'
  vertices: number
  faces: number
}

type Json = Record<string, unknown>
const fail = (): never => { throw new Error('The 3D artifact is outside the supported, self-contained Atlas format.') }
function object(value: unknown): Json {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail()
  return value as Json
}
function array(value: unknown, maximum: number, minimum = 0): unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) return fail()
  return value
}
function integer(value: unknown, maximum: number, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) return fail()
  return value
}
function selfContained(value: unknown, depth = 0): void {
  if (depth > 32) fail()
  if (!value || typeof value !== 'object') return
  if (Array.isArray(value)) value.forEach(child => selfContained(child, depth + 1))
  else {
    const record = object(value)
    if ('uri' in record) fail()
    if (record.extensions && Object.keys(object(record.extensions)).some(key => key !== 'KHR_materials_unlit')) fail()
    Object.values(record).forEach(child => selfContained(child, depth + 1))
  }
}

export function validateAtlasGlb(data: ArrayBuffer): AtlasGeometry {
  if (data.byteLength < 28 || data.byteLength > 16 * 1024 * 1024) fail()
  const header = new DataView(data)
  if (header.getUint32(0, true) !== 0x46546c67 || header.getUint32(4, true) !== 2 ||
      header.getUint32(8, true) !== data.byteLength || header.getUint32(16, true) !== 0x4e4f534a) fail()
  const jsonLength = integer(header.getUint32(12, true), Math.min(1024 * 1024, data.byteLength - 28), 4)
  if (jsonLength % 4) fail()
  const document = object(JSON.parse(new TextDecoder().decode(data.slice(20, 20 + jsonLength))))
  const binaryStart = 28 + jsonLength
  const length = header.getUint32(20 + jsonLength, true)
  if (header.getUint32(24 + jsonLength, true) !== 0x004e4942 || length % 4 || binaryStart + length !== data.byteLength) fail()
  selfContained(document)
  if (object(document.asset).version !== '2.0' || document.animations || document.skins || document.cameras || document.extensions) fail()
  for (const name of ['extensionsUsed', 'extensionsRequired']) {
    if (array(document[name] ?? [], 1).some(value => value !== 'KHR_materials_unlit')) fail()
  }
  const buffers = array(document.buffers, 1, 1)
  if (object(buffers[0]).byteLength !== length) fail()
  const views = array(document.bufferViews, 64, 1).map(value => {
    const view = object(value)
    if (view.buffer !== 0 || view.extensions) fail()
    const offset = integer(view.byteOffset ?? 0, length)
    const size = integer(view.byteLength, length, 1)
    if (offset + size > length) fail()
    return { byteStride: view.byteStride, offset, size }
  })
  const accessors = array(document.accessors, 64, 1).map(object)
  const read = (index: unknown, shape: string, componentType: number, normalized = false) => {
    const accessor = accessors[integer(index, accessors.length - 1)]
    if (accessor.type !== shape || accessor.componentType !== componentType || accessor.sparse ||
        accessor.extensions || (accessor.normalized ?? false) !== normalized) fail()
    const count = integer(accessor.count, 600000, 1)
    const view = views[integer(accessor.bufferView, views.length - 1)]
    const width = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[shape] ?? fail()
    const bytes = componentType === 5121 ? 1 : componentType === 5123 ? 2 : 4
    const stride = width * bytes
    const offset = integer(accessor.byteOffset ?? 0, view.size)
    if ((view.byteStride ?? stride) !== stride || offset + count * stride > view.size || (view.offset + offset) % bytes) fail()
    const value = (index: number) => {
      const start = binaryStart + view.offset + offset + index * bytes
      return componentType === 5126 ? header.getFloat32(start, true) :
        componentType === 5125 ? header.getUint32(start, true) :
          componentType === 5123 ? header.getUint16(start, true) : header.getUint8(start)
    }
    if (componentType === 5126) for (let index = 0; index < count * width; index++) {
      if (!Number.isFinite(value(index))) fail()
    }
    return { count, value }
  }
  const nodes = array(document.nodes, 1, 1).map(object)
  if (nodes[0].mesh !== 0 || Object.keys(nodes[0]).some(key => !['mesh', 'name'].includes(key))) fail()
  const scenes = array(document.scenes, 1, 1).map(object)
  if ((document.scene ?? 0) !== 0 || JSON.stringify(scenes[0].nodes) !== '[0]') fail()
  const mesh = object(array(document.meshes, 1, 1)[0])
  const primitives = array(mesh.primitives, 8, 1).map(object)
  const sparse = primitives.length === 1 && primitives[0].mode === 0
  let vertices = 0, faces = 0
  const materials = array(document.materials ?? [], 8).map(object)
  for (const primitive of primitives) {
    if (primitive.targets || primitive.extensions) fail()
    const attributes = object(primitive.attributes)
    const allowed = sparse ? ['POSITION', 'COLOR_0'] : ['POSITION', 'TEXCOORD_0', 'NORMAL']
    if (Object.keys(attributes).some(key => !allowed.includes(key))) fail()
    const xyz = read(attributes.POSITION, 'VEC3', 5126)
    vertices += xyz.count
    if (sparse) {
      if ('indices' in primitive || 'material' in primitive || xyz.count > 120000) fail()
      if (read(attributes.COLOR_0, 'VEC4', 5121, true).count !== xyz.count) fail()
    } else {
      if ((primitive.mode ?? 4) !== 4) fail()
      integer(primitive.material, materials.length - 1)
      const component = accessors[integer(primitive.indices, accessors.length - 1)].componentType
      if (component !== 5123 && component !== 5125) fail()
      const indices = read(primitive.indices, 'SCALAR', component as number)
      if (indices.count % 3) fail()
      for (let index = 0; index < indices.count; index++) if (indices.value(index) >= xyz.count) fail()
      faces += indices.count / 3
      if (read(attributes.TEXCOORD_0, 'VEC2', 5126).count !== xyz.count) fail()
      if ('NORMAL' in attributes && read(attributes.NORMAL, 'VEC3', 5126).count !== xyz.count) fail()
    }
  }
  if (vertices > 600000 || (!sparse && (faces < 50 || faces > 200000))) fail()
  const images = array(document.images ?? [], 8).map(object)
  const textures = array(document.textures ?? [], 8).map(object)
  const samplers = array(document.samplers ?? [], 8)
  if (sparse && (images.length || textures.length || materials.length || samplers.length)) fail()
  if (!sparse && (!images.length || !textures.length || !materials.length)) fail()
  let pixels = 0
  for (const image of images) {
    if (image.mimeType !== 'image/png') fail()
    const view = views[integer(image.bufferView, views.length - 1)]
    const offset = binaryStart + view.offset
    if (view.size < 33 || header.getUint32(offset) !== 0x89504e47 || header.getUint32(offset + 4) !== 0x0d0a1a0a ||
        header.getUint32(offset + 8) !== 13 || header.getUint32(offset + 12) !== 0x49484452) fail()
    const width = integer(header.getUint32(offset + 16), 4096, 1)
    const height = integer(header.getUint32(offset + 20), 4096, 1)
    pixels += width * height
    if (pixels > 16777216) fail()
  }
  for (const texture of textures) {
    integer(texture.source, images.length - 1)
    if ('sampler' in texture) integer(texture.sampler, samplers.length - 1)
  }
  for (const material of materials) {
    if (!('KHR_materials_unlit' in object(material.extensions))) fail()
    const base = object(object(material.pbrMetallicRoughness).baseColorTexture)
    integer(base.index, textures.length - 1)
    if ((base.texCoord ?? 0) !== 0) fail()
  }
  return { representation: sparse ? 'sparse_point_cloud' : 'textured_mesh', vertices, faces }
}
