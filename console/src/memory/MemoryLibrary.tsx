import { useEffect, useState } from 'react'
import type { AtlasClient } from '../atlas/client'
import type { Capture, Space, SpaceDetail } from '../atlas/types'
import { Icon } from '../atlas/Icon'
import MemoryDialog from './MemoryDialog'
import './memory.css'

export default function MemoryLibrary({ client }: { client?: AtlasClient }) {
  const [spaces, setSpaces] = useState<Space[]>([])
  const [selected, setSelected] = useState('')
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<SpaceDetail | null>(null)
  const [capture, setCapture] = useState<Capture | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(Boolean(client))
  const [reload, setReload] = useState(0)
  useEffect(() => {
    if (!client) return
    const controller = new AbortController()
    void client
      .list(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setSpaces(value)
          setError('')
          setLoading(false)
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(error instanceof Error ? error.message : 'Could not load spaces.')
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [client, reload])
  useEffect(() => {
    if (!client || !selected) return
    const controller = new AbortController()
    void client
      .detail(selected, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setDetail(value)
          setError('')
          setLoading(false)
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(error instanceof Error ? error.message : 'Could not load captures.')
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [client, selected, reload])
  return (
    <section className="memory-library" aria-label="World memories">
      <div className="memory-library-hero">
        <span className="atlas-eyebrow">PLACES HAVE STORIES</span>
        <h2>Keep the feeling of being there.</h2>
        <p>
          A scan holds the shape of a place. Add the creek’s sound, a friend’s story, or the warmth
          of that afternoon—grounded in your originals.
        </p>
        <div className="memory-library-steps">
          <span>
            <Icon name="camera" />
            Choose a capture
          </span>
          <span>
            <Icon name="spark" />
            Add its context
          </span>
          <span>
            <Icon name="people" />
            Share with your space
          </span>
        </div>
      </div>
      <p className="memory-callout">
        Start with a real photo, panorama, or video uploaded in Spaces. Memory context stays beside
        it; it never changes 3D geometry or claims a generated world is a verified record.
      </p>
      {!client ? (
        <p role="status">Connect an Atlas workspace in Spaces to open your memories.</p>
      ) : (
        <>
          <details
            className="memory-space-picker"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.currentTarget.open = false
                event.currentTarget.querySelector('summary')?.focus()
              }
            }}
          >
            <summary aria-label="Choose a space">
              {spaces.find((space) => space.id === selected)?.title || 'Choose a space'}
            </summary>
            <label>
              Find a place
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search your spaces"
              />
            </label>
            <div role="group" aria-label="Available spaces">
              {spaces
                .filter((space) =>
                  space.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
                )
                .map((space) => (
                  <button
                    key={space.id}
                    aria-pressed={selected === space.id}
                    onClick={(event) => {
                      setSelected(space.id)
                      setDetail(null)
                      setCapture(null)
                      setLoading(true)
                      const picker = event.currentTarget.closest('details')!
                      picker.open = false
                      picker.querySelector('summary')?.focus()
                    }}
                  >
                    {space.title}
                  </button>
                ))}
              {spaces.length > 0 &&
                !spaces.some((space) =>
                  space.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
                ) && <p>No matching spaces. Try another name.</p>}
            </div>
          </details>
          {error && (
            <p role="alert">
              {error}{' '}
              <button
                onClick={() => {
                  setLoading(true)
                  setReload((value) => value + 1)
                }}
              >
                Try again
              </button>
            </p>
          )}
          {loading && <p role="status">Opening your memories…</p>}
          {!loading && !spaces.length && !error && (
            <p>No spaces yet. Create a place in Spaces and add your first capture.</p>
          )}
          {detail && (
            <div className="memory-library-grid">
              {detail.captures.length ? (
                detail.captures.map((item) => (
                  <button className="memory-capture" key={item.id} onClick={() => setCapture(item)}>
                    <Icon name={item.kind === 'video' ? 'video' : 'camera'} size={26} />
                    <span className="atlas-eyebrow">
                      {item.kind} · {item.source}
                    </span>
                    <strong>{item.note || `${item.name}’s capture`}</strong>
                    <span>
                      {item.captured_at == null
                        ? 'Capture time unknown'
                        : new Date(item.captured_at).toLocaleString()}
                    </span>
                    <span className="memory-capture-link">Open memory & sounds ↗</span>
                  </button>
                ))
              ) : (
                <p>
                  This space has no captures yet. Add a photo or video from its capture page in
                  Spaces.
                </p>
              )}
            </div>
          )}
          {capture && (
            <MemoryDialog
              client={client}
              spaceId={selected}
              capture={capture}
              onClose={() => setCapture(null)}
            />
          )}
        </>
      )}
    </section>
  )
}
