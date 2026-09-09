import { useState } from 'react'
import { AtlasDialog } from '../atlas/AtlasDialog'
import type { AtlasClient } from '../atlas/client'
import type { Capture } from '../atlas/types'
import { MemoryPanel } from './MemoryPanel'

export default function MemoryDialog({
  client,
  spaceId,
  capture,
  onClose,
}: {
  client: AtlasClient
  spaceId: string
  capture: Capture
  onClose: () => void
}) {
  const [dirty, setDirty] = useState(false)
  return (
    <AtlasDialog
      title="Your memory"
      className="memory-dialog"
      onClose={() => {
        if (!dirty || window.confirm('Leave without saving your memory details?')) onClose()
      }}
    >
      <MemoryPanel
        key={`${spaceId}/${capture.id}`}
        client={client}
        spaceId={spaceId}
        capture={capture}
        onDirtyChange={setDirty}
        onDone={onClose}
      />
    </AtlasDialog>
  )
}
