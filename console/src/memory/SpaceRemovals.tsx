import { useEffect, useState } from 'react'
import type { AtlasClient } from '../atlas/client'
import { AtlasDialog } from '../atlas/AtlasDialog'
import { memoryActor } from './types'
import type { RemovalDirectory } from './removal'
import { REMOVAL_LIMITS } from './MemoryRemoval'
import './removal.css'

export default function SpaceRemovals({ client, spaceId, onClose }: { client: AtlasClient; spaceId: string; onClose: () => void }) {
  const [page, setPage] = useState<RemovalDirectory | null>(null)
  const [before, setBefore] = useState<number>()
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function update() {
      try {
        const result = await client.removals(spaceId, controller.signal, before)
        if (!controller.signal.aborted) { setPage(result); setError('') }
        // Continue checking access even for completed receipts while the dialog is open.
        if (!controller.signal.aborted) timer = setTimeout(() => void update(), 10_000)
      } catch (error) {
        if (!controller.signal.aborted) { setPage(null); setError(error instanceof Error ? error.message : 'Receipts are unavailable.') }
      }
    }
    void update()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [client, spaceId, before, revision])
  function refresh() { setPage(null); setError(''); setRevision(value => value + 1) }
  return <AtlasDialog title="Removal status" onClose={onClose}>
    <section className="memory-removal" aria-label="Removal receipts">
      <h3>Know what’s gone. Know what’s pending.</h3>
      <p>Access to a removed source is withdrawn immediately. Cleanup can take longer while transfers or world builds finish.</p>
      <p className="atlas-fine">{REMOVAL_LIMITS}</p>
      {error && <p role="alert">{error}</p>}
      {!page && !error && <p role="status">Checking removal receipts…</p>}
      {page && <>
        <p role="status">{page.pending} cleanup pending · {page.completed} local cleanup complete · {page.scope === 'own' ? 'Your contributions only' : 'This Space'}</p>
        {page.receipts.length === 0 && <p>No removal receipts on this page.</p>}
        <ol className="removal-receipts">{page.receipts.map(receipt => <li key={receipt.capture_id}>
          <strong>{receipt.state === 'local_removed' ? 'Local cleanup complete' : 'Access withdrawn · cleanup pending'}</strong>
          <span>Capture {receipt.capture_id}</span>
          <span>{new Date(receipt.requested_at).toLocaleString()} · {memoryActor(receipt.requested_by)}</span>
          <span>{receipt.recordings} recordings · {receipt.builds} shared builds</span>
          {receipt.completed_at !== null && <span>Completed {new Date(receipt.completed_at).toLocaleString()}</span>}
        </li>)}</ol>
        {page.pending > 0 && <p>If cleanup stays pending, ask the workspace operator to verify unfinished transfers or workers. A timeout is not proof that they stopped. Status refreshes here every 10 seconds; it cannot force completion.</p>}
      </>}
      <div className="memory-actions">
        <button className="atlas-secondary" onClick={refresh}>Refresh status</button>
        {before !== undefined && <button className="atlas-secondary" onClick={() => { setPage(null); setBefore(undefined) }}>Newest receipts</button>}
        {page?.next_before != null && <button className="atlas-secondary" onClick={() => { setPage(null); setBefore(page.next_before!) }}>Older receipts</button>}
      </div>
    </section>
  </AtlasDialog>
}
