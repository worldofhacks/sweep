import { useState } from 'react'
import { AtlasDialog } from '../atlas/AtlasDialog'
import type { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { MemoryPanel } from './MemoryPanel'
import MemoryRemoval from './MemoryRemoval'

export default function MemoryDialog({
  client,
  spaceId,
  capture,
  onClose,
  onRemoved,
}: {
  client: AtlasClient
  spaceId: string
  capture: Capture
  onClose: () => void
  onRemoved?: () => void
}) {
  const [dirty, setDirty] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [removalBusy, setRemovalBusy] = useState(false)
  return (
    <AtlasDialog
      title={removing ? 'Remove a memory' : 'Your memory'}
      className="memory-dialog"
      onClose={() => {
        if (removalBusy) return
        if (!dirty || window.confirm('Leave without saving your memory details?')) onClose()
      }}
    >
      {removing && onRemoved ? <MemoryRemoval client={client} spaceId={spaceId} captureId={capture.id} onRemoved={onRemoved} onCancel={() => setRemoving(false)} onBusy={setRemovalBusy} /> : <MemoryPanel
        key={`${spaceId}/${capture.id}`}
        client={client}
        spaceId={spaceId}
        capture={capture}
        onDirtyChange={setDirty}
        onDone={onClose}
        onRemove={onRemoved ? () => { if (!dirty) setRemoving(true) } : undefined}
      />}
    </AtlasDialog>
  )
}
