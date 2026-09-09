import { Icon } from '../atlas/Icon'
import type { ReactNode } from 'react'

/** One persistent identity across Spaces and every operational module. */
export function WorkspaceHeader({ onOpenFleet, controls }: { onOpenFleet: () => void; controls?: ReactNode }) {
  return (
    <header className="sh-atlas-header" aria-label="Workspace header">
      <span className="sh-brand">
        <Icon name="spaces" size={23} />
        <strong>sweep</strong>
        <span>ATLAS</span>
      </span>
      <span>See more. Understand together.</span>
      {controls ?? <button className="sh-atlas-fleet" onClick={onOpenFleet}>
        Fleet workspace <Icon name="arrow" size={14} />
      </button>}
    </header>
  )
}
