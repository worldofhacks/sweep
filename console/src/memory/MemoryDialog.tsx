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
      title="The story of this moment"
      onClose={() => {
        if (!dirty || window.confirm('Leave without saving your memory details?')) onClose()
      }}
    >
      <MemoryPanel client={client} spaceId={spaceId} capture={capture} onDirtyChange={setDirty} />
    </AtlasDialog>
  )
}
