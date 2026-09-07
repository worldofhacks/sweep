/// <reference types="node" />
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MAX_IMAGE_BYTES, loadLocalDraft, loadOccupancyImage, verifyDraftImage } from './files'
import { draftFixture } from './test-fixtures'
import type { MapDraft } from './types'

let decodedSize: { width: number; height: number } | null
let decodeFails: boolean
let decodedUrls: string[]

/** jsdom does not decode images. Read the actual PNG IHDR while controlling decoder failures. */
class TestImage {
  naturalWidth = 0
  naturalHeight = 0
  onload: (() => void) | null = null
  onerror: (() => void) | null = null

  set src(dataUrl: string) {
    decodedUrls.push(dataUrl)
    queueMicrotask(() => {
      try {
        const bytes = bytesFromDataUrl(dataUrl)
        if (decodeFails || bytes.length < 24 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') throw new Error('PNG decode failed')
        this.naturalWidth = decodedSize?.width ?? bytes.readUInt32BE(16)
        this.naturalHeight = decodedSize?.height ?? bytes.readUInt32BE(20)
        this.onload?.()
      } catch {
        this.onerror?.()
      }
    })
  }
}

function bytesFromDataUrl(dataUrl: string): Buffer {
  return Buffer.from(dataUrl.slice(dataUrl.indexOf(',') + 1), 'base64')
}

function imageFile(type = 'image/png'): File {
  return new File([new Uint8Array(bytesFromDataUrl(draftFixture().image!.dataUrl))], 'actual-test-map.png', { type })
}

function draftFile(draft: unknown): File {
  return new File([JSON.stringify(draft)], 'local-map.json', { type: 'application/json' })
}

beforeEach(() => {
  decodedSize = null
  decodeFails = false
  decodedUrls = []
  vi.stubGlobal('Image', TestImage)
  // Exercise SHA-256 itself; this is the native implementation, not a canned digest.
  vi.stubGlobal('crypto', webcrypto)
})

afterEach(() => vi.unstubAllGlobals())

describe('occupancy image bytes', () => {
  it('hashes the actual file bytes and reads the fixture PNG dimensions', async () => {
    const fixture = draftFixture().image!
    const independentDigest = createHash('sha256').update(bytesFromDataUrl(fixture.dataUrl)).digest('hex')
    expect(independentDigest).toBe(fixture.sha256)
    const result = await loadOccupancyImage(imageFile())
    expect(result).toEqual({ ...fixture, name: 'actual-test-map.png' })
    expect(result.sha256).toBe(independentDigest)
    expect(decodedUrls).toEqual([fixture.dataUrl])
  })

  it.each(['image/svg+xml', 'text/plain', 'application/octet-stream', ''])('rejects unsupported MIME %j before reading or decoding', async (type) => {
    await expect(loadOccupancyImage(imageFile(type))).rejects.toThrow('Choose a PNG or JPEG')
    expect(decodedUrls).toEqual([])
  })

  it('rejects files over 8 MiB before decoding', async () => {
    const file = new File([new Uint8Array(MAX_IMAGE_BYTES + 1)], 'too-large.png', { type: 'image/png' })
    await expect(loadOccupancyImage(file)).rejects.toThrow('no larger than 8 MiB')
    expect(decodedUrls).toEqual([])
  })

  it('rejects malformed bytes even when the file advertises PNG', async () => {
    const file = new File(['not a PNG'], 'invalid.png', { type: 'image/png' })
    await expect(loadOccupancyImage(file)).rejects.toThrow('could not be decoded')
  })

  it('propagates an actual image decoder failure for an otherwise bounded file', async () => {
    decodeFails = true
    await expect(loadOccupancyImage(imageFile())).rejects.toThrow('could not be decoded')
  })

  it.each([{ width: 0, height: 100 }, { width: 100, height: 0 }, { width: 4097, height: 4096 }])('rejects unusable decoded dimensions %j', async (size) => {
    decodedSize = size
    await expect(loadOccupancyImage(imageFile())).rejects.toThrow('16 megapixels')
  })
})

describe('shared saved-draft image verification', () => {
  it('accepts an image only when its declared dimensions and digest match its bytes', async () => {
    const draft = draftFixture()
    await expect(verifyDraftImage(draft)).resolves.toEqual(draft)
    expect(decodedUrls).toEqual([draft.image!.dataUrl])
  })

  it.each(['sha256', 'width', 'height'] as const)('rejects an incorrect declared %s', async (field) => {
    const draft = draftFixture()
    if (field === 'sha256') draft.image!.sha256 = '0'.repeat(64)
    else draft.image![field] += 1
    await expect(verifyDraftImage(draft)).rejects.toThrow('dimensions or hash do not match its bytes')
  })

  it('rejects altered raster bytes with unchanged declared hash and dimensions', async () => {
    const draft = draftFixture()
    const bytes = bytesFromDataUrl(draft.image!.dataUrl)
    bytes[bytes.length - 1] ^= 1
    draft.image!.dataUrl = `data:image/png;base64,${bytes.toString('base64')}`
    // The controlled decoder still reports the unchanged IHDR; the real digest must catch this.
    await expect(verifyDraftImage(draft)).rejects.toThrow('dimensions or hash do not match its bytes')
  })

  it('enforces the decoded-byte limit even when called independently of file import', async () => {
    const draft = draftFixture()
    const oversized = Buffer.alloc(MAX_IMAGE_BYTES + 1)
    bytesFromDataUrl(draft.image!.dataUrl).copy(oversized)
    draft.image!.dataUrl = `data:image/png;base64,${oversized.toString('base64')}`
    await expect(verifyDraftImage(draft)).rejects.toThrow('limited to 8 MiB')
  })

  it('leaves a draft with no image unqualified rather than generating an image', async () => {
    const draft: MapDraft = { ...draftFixture(), image: null }
    await expect(verifyDraftImage(draft)).resolves.toEqual(draft)
    expect(decodedUrls).toEqual([])
  })
})

describe('local draft file integration', () => {
  it('verifies actual imported bytes and removes imported relay authority fields', async () => {
    const draft = draftFixture()
    const imported = await loadLocalDraft(draftFile({ ...draft, approved: true, approval: { auditId: 'not-authority' }, validation: { valid: true }, reference: { revision: 'not-authority' } }))
    expect(imported).toEqual(draft)
    expect(imported).not.toHaveProperty('approval')
    expect(imported).not.toHaveProperty('validation')
    expect(imported).not.toHaveProperty('reference')
    expect(decodedUrls).toEqual([draft.image!.dataUrl])
  })

  it.each(['sha256', 'width', 'height'] as const)('rejects imported image metadata with the wrong %s', async (field) => {
    const draft = draftFixture()
    if (field === 'sha256') draft.image!.sha256 = 'f'.repeat(64)
    else draft.image![field] += 1
    await expect(loadLocalDraft(draftFile(draft))).rejects.toThrow('dimensions or hash do not match its bytes')
  })

  it.each(['https://unconfigured.example/map.png', 'data:image/svg+xml;base64,PHN2Zy8+', 'javascript:alert(1)', 'data:image/png;base64,%%%'])('rejects unsafe or malformed imported image source %j without decoding it', async (dataUrl) => {
    const draft = draftFixture()
    draft.image!.dataUrl = dataUrl
    await expect(loadLocalDraft(draftFile(draft))).rejects.toThrow('Invalid or oversized')
    expect(decodedUrls).toEqual([])
  })

  it('rejects malformed JSON without decoding image content', async () => {
    await expect(loadLocalDraft(new File(['{invalid'], 'broken.json'))).rejects.toThrow()
    expect(decodedUrls).toEqual([])
  })

  it('rejects local documents over 16 MiB before reading image content', async () => {
    const file = new File([new Uint8Array(16 * 1024 * 1024 + 1)], 'too-large.json')
    await expect(loadLocalDraft(file)).rejects.toThrow('Invalid or oversized')
    expect(decodedUrls).toEqual([])
  })
})
