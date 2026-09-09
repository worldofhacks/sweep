import { useEffect, useState } from 'react'
import './captured-map.css'

type Preview = { version: 1; title: string; description: string; ownerApproved: boolean; images: { file: string; caption: string }[] }

function readPreview(value: unknown): Preview {
  if (!value || typeof value !== 'object' || !('version' in value) || value.version !== 1
    || !('title' in value) || typeof value.title !== 'string' || value.title.length > 160
    || !('description' in value) || typeof value.description !== 'string' || value.description.length > 2000
    || !('images' in value) || !Array.isArray(value.images) || value.images.length < 1 || value.images.length > 4) {
    throw new Error('Invalid captured map manifest.')
  }
  const images = value.images.map((image: unknown) => {
    if (!image || typeof image !== 'object' || !('file' in image) || typeof image.file !== 'string'
      || !/^[a-z0-9][a-z0-9-]{0,100}\.png$/.test(image.file)
      || !('caption' in image) || typeof image.caption !== 'string' || image.caption.length > 300) {
      throw new Error('Invalid captured map image.')
    }
    return { file: image.file, caption: image.caption }
  })
  const ownerApproved = 'ownerApproved' in value && value.ownerApproved === true
  return { version: 1, title: value.title, description: value.description, ownerApproved, images }
}

const directory = `${import.meta.env.BASE_URL}mapping-preview/`

export function CapturedMap() {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [selected, setSelected] = useState(0)
  const [zoom, setZoom] = useState(100)
  const [imageFailed, setImageFailed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    void fetch(`${directory}manifest.json`, { signal: controller.signal, credentials: 'same-origin', cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error('Captured map unavailable.')
        return readPreview(await response.json())
      })
      .then((value) => { if (!controller.signal.aborted) setPreview(value) })
      .catch(() => { if (!controller.signal.aborted) setUnavailable(true) })
    return () => controller.abort()
  }, [])

  if (unavailable) return <p role="status" className="mp-status">No captured map is installed on this console.</p>
  if (!preview) return <p role="status" className="mp-status">Loading captured map…</p>
  const current = preview.images[selected]
  return <section className="cm" aria-label="Captured map preview">
    <header><h2>{preview.title}</h2><p className="cm-status">{preview.ownerApproved
      ? 'Map approved · flight setup pending'
      : 'Provisional · awaiting calibration and map approval'}</p></header>
    <p>{preview.description}</p>
    <p className="cm-boundary">Static capture evidence. This view does not enable navigation or flight.</p>
    <div className="cm-toolbar">
      <div role="group" aria-label="Captured map layers">
        {preview.images.map((image, index) => <button type="button" key={index} aria-pressed={selected === index} onClick={() => { setSelected(index); setImageFailed(false); setZoom(100) }}>{image.caption}</button>)}
      </div>
      <label>Zoom <input type="range" min="100" max="300" step="25" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} />{zoom}%</label>
      <a href={`${directory}${current.file}`} target="_blank" rel="noreferrer">Open full image</a>
    </div>
    <div className="cm-viewport" tabIndex={0} role="region" aria-label="Scrollable captured map">
      {imageFailed ? <p role="status">This captured map image could not be loaded.</p> : <img key={current.file} src={`${directory}${current.file}`} alt={current.caption} style={{ width: `${zoom}%` }} onError={() => setImageFailed(true)} />}
    </div>
  </section>
}
