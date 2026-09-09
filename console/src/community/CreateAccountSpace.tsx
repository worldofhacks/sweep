import { lazy, Suspense, useState } from 'react'
import { locate } from '../atlas/client'
import { Icon } from '../atlas/Icon'
import type { NewSpace, Space } from '../atlas/types'
import type { AccountClient, AccountGrant } from './accountClient'

const SpaceMap = lazy(() => import('../atlas/SpaceMap'))
const empty = () => {}

/** Keep the same submission ID and payload after an ambiguous network failure. */
export default function CreateAccountSpace({ api, onCreated, onClose }: {
  api: AccountClient; onCreated: (grant: AccountGrant) => void; onClose: () => void
}) {
  const [title, setTitle] = useState('')
  const [place, setPlace] = useState('')
  const [description, setDescription] = useState('')
  const [coordinates, setCoordinates] = useState<[string, string]>(['', ''])
  const [center, setCenter] = useState<[number, number]>([-97.7431, 30.2672])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState<{ id: string; space: NewSpace } | null>(null)
  const latitude = Number(coordinates[0]), longitude = Number(coordinates[1])
  const hasLocation = coordinates.every(v => v.trim() !== '') && Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 85 && Math.abs(longitude) <= 180
  const valid = title.trim().length >= 3 && place.trim().length > 0 && hasLocation
  const preview: Space[] = hasLocation ? [{ id: 'account-draft', title: title.trim() || 'Your chosen place', place,
    description, category: 'community', latitude, longitude, radius: 80,
    created_at: 0, updated_at: 0, status: 'active', verification: 'unverified', capture_count: 0, coverage_percent: 0, contributors: 0 }] : []
  function pick(lng: number, lat: number) {
    if (busy || attempt) return
    setCoordinates([lat.toFixed(6), lng.toFixed(6)]); setCenter([lng, lat])
  }
  function recenterChosenLocation() { if (hasLocation) setCenter([longitude, latitude]) }
  async function currentLocation() {
    setBusy(true); setError('')
    try {
      const value = await locate()
      setCoordinates([value.latitude.toFixed(6), value.longitude.toFixed(6)])
      setCenter([value.longitude, value.latitude])
    } catch (error) { setError(error instanceof Error ? error.message : 'Choose a place on the map instead.') }
    finally { setBusy(false) }
  }
  async function create() {
    if (busy || (!attempt && !valid)) return
    const submission = attempt ?? { id: crypto.randomUUID(), space: {
      title: title.trim(), place: place.trim(), description: description.trim(), category: 'community' as const,
      latitude, longitude, radius: 80,
    } }
    setAttempt(submission); setBusy(true); setError('')
    try { onCreated(await api.create(submission.id, submission.space)) }
    catch (error) { setError(error instanceof Error ? error.message : 'Creation could not be confirmed.') }
    finally { setBusy(false) }
  }
  return <section className="account-create" aria-label="Create your space">
    <div className="account-page-heading"><div><span className="atlas-eyebrow">START WITH A PLACE YOU LOVE</span><h2>A little world of your own.</h2><p>Make a home for your memories. Invite your people when you’re ready.</p></div><Icon name="spaces" size={32} /></div>
    <form onSubmit={event => { event.preventDefault(); void create() }}>
      <fieldset disabled={busy || Boolean(attempt)} className="atlas-fieldset">
        <label className="atlas-field">Space name<input required minLength={3} maxLength={100} value={title} onChange={event => setTitle(event.target.value)} placeholder="Our weekends by the lake" /></label>
        <label className="atlas-field">Place name<input required maxLength={120} value={place} onChange={event => setPlace(event.target.value)} placeholder="Lady Bird Lake, Austin, Texas" /></label>
        <label className="atlas-field">What makes it special? <span className="atlas-muted">Optional</span><textarea maxLength={2000} rows={3} value={description} onChange={event => setDescription(event.target.value)} placeholder="The stories, little rituals, and people that belong here." /></label>
        <button type="button" className="atlas-secondary" onClick={() => void currentLocation()}><Icon name="pin" size={16} />Use my current location</button>
        <p className="atlas-fine">Or choose a point on the map. Austin is the starting view, not an assumed location. This sets the Space’s location; it does not start live tracking.</p>
      </fieldset>
      <div className="account-create-map"><Suspense fallback={<p role="status">Opening the map…</p>}><SpaceMap spaces={preview} detail={null} center={center} overviewZoom={hasLocation ? 17.5 : 11} pickingZoom={hasLocation ? 17.5 : 11} picking={!busy && !attempt}
        coverageVisible={false} selectedCell={null} position={null} onPick={pick} onSelect={empty} onCell={empty} /></Suspense></div>
      <p role="status" className="atlas-fine">{hasLocation ? `Chosen location: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}` : 'Choose a location before creating your Space.'}</p>
      <details><summary>Enter coordinates instead</summary><fieldset disabled={busy || Boolean(attempt)} className="atlas-fieldset account-coordinate-fields">
        <label className="atlas-field">Latitude<input type="number" step="any" min={-85} max={85} value={coordinates[0]} onBlur={recenterChosenLocation} onChange={event => setCoordinates([event.target.value, coordinates[1]])} /></label>
        <label className="atlas-field">Longitude<input type="number" step="any" min={-180} max={180} value={coordinates[1]} onBlur={recenterChosenLocation} onChange={event => setCoordinates([coordinates[0], event.target.value])} /></label>
      </fieldset></details>
      <p className="account-hint">This Space isn’t public. Only you, people you explicitly invite, and the service’s workspace administrators can access it. Invitations include its location and future captures. Account deletion and ownership transfer aren’t available yet—keep your original media.</p>
      {error && <p role="alert">{error}{attempt && ' Your draft is held unchanged. Retry safely, or check your spaces before starting another.'}</p>}
      <div className="account-create-actions"><button type="button" className="atlas-secondary" disabled={busy} onClick={onClose}>{attempt ? 'Check my spaces' : 'Not now'}</button>
        <button className="atlas-primary" disabled={busy || (!valid && !attempt)}>{busy ? 'Working…' : attempt ? 'Retry this draft' : 'Create my space'}<Icon name="arrow" size={16} /></button></div>
    </form>
  </section>
}
