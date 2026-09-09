import { useEffect, useRef, useState } from 'react'
import type { AtlasClient } from '../atlas/client'
import type { RemovalPreview } from './removal'
import './removal.css'

export const REMOVAL_LIMITS = 'This covers managed files and the active local database. Backups, files people downloaded, copies on phones, and data already sent to a provider are not erased by this action.'

export default function MemoryRemoval({ client, spaceId, captureId, onRemoved, onCancel, onBusy }: {
  client: AtlasClient; spaceId: string; captureId: string; onRemoved: () => void; onCancel: () => void; onBusy: (busy: boolean) => void
}) {
  const [preview, setPreview] = useState<RemovalPreview | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const mounted = useRef(false)
  const sending = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const removed = useRef(onRemoved)
  useEffect(() => { removed.current = onRemoved }, [onRemoved])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => {
    if (!busy) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [busy])
  useEffect(() => {
    const controller = new AbortController()
    void client.removal(spaceId, captureId, controller.signal).then(result => {
      if (controller.signal.aborted) return
      if (result.state !== 'preview') removed.current()
      else setPreview(result)
    }).catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Status unavailable.') })
    return () => controller.abort()
  }, [client, spaceId, captureId, revision])
  async function remove() {
    if (!preview || !confirmed || sending.current) return
    sending.current = true; setBusy(true); onBusy(true); setError(''); setConfirmed(false)
    try {
      await client.removeCapture(spaceId, captureId, preview.confirmation)
      if (mounted.current) removed.current()
    } catch {
      // The server may have accepted a request whose response never arrived.
      // Never replay a destructive request automatically, including after a conflict.
      if (!mounted.current) return
      setPreview(null)
      try {
        const status = await client.removal(spaceId, captureId)
        if (!mounted.current) return
        if (status.state !== 'preview') removed.current()
        else { setPreview(status); setError('Removal was not confirmed. Review the current impact and confirm again if you still want to remove it.') }
      } catch {
        if (mounted.current) setError('We could not verify whether removal finished. Check status before trying again; no request will be repeated automatically.')
      }
    } finally {
      sending.current = false
      if (mounted.current) { setBusy(false); onBusy(false) }
    }
  }
  return <section className="memory-removal" aria-label="Review memory removal" aria-busy={busy}>
    <p className="atlas-eyebrow">YOUR MEMORY · YOUR CHOICE</p>
    <h3 ref={heading} tabIndex={-1}>Review what leaves this Space.</h3>
    <p>This withdraws the original capture, its story, recordings, AI context, and edit and date history from everyone in this Space. There is no undo here.</p>
    {!preview && !error && <p role="status">Checking the current impact…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <>
      <div className="removal-impact"><strong>1 original · {preview.recordings} {preview.recordings === 1 ? 'recording' : 'recordings'}</strong><span>{preview.builds} shared 3D {preview.builds === 1 ? 'build' : 'builds'} will also be withdrawn.</span></div>
      <p>Other original captures stay. Affected worlds can be rebuilt from the remaining captures.</p>
      {preview.analysis_pending && <p>Analysis is still running or interrupted. Access is withdrawn first; cleanup waits for confirmed completion of work that used this source.</p>}
      <p className="atlas-fine">{REMOVAL_LIMITS}</p>
      <label className="removal-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} /><span>I understand this withdraws the original, its details, and any affected shared builds.</span></label>
    </>}
    <div className="memory-actions">
      <button className="atlas-secondary" disabled={busy} onClick={onCancel}>Back to memory</button>
      {preview ? <button className="atlas-primary" disabled={!confirmed || busy} onClick={() => void remove()}>{busy ? 'Withdrawing access…' : 'Remove from this Space'}</button> : <button className="atlas-secondary" disabled={busy} onClick={() => { setError(''); setRevision(value => value + 1) }}>Check removal status</button>}
    </div>
    <p className="atlas-fine">A receipt stays in Captures → Removal status, even after the memory disappears.</p>
  </section>
}
