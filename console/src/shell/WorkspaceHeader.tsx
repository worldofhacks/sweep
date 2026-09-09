import { Icon } from '../atlas/Icon'
import type { ReactNode } from 'react'
import { AccountButton } from '../community/AccountButton'

/** One persistent identity across Spaces and every operational module. */
export function WorkspaceHeader({ onOpenFleet, controls }: { onOpenFleet: () => void; controls?: ReactNode }) {
  return (
    <header className="sh-atlas-header" aria-label="Workspace header">
      <span className="sh-brand">
        <Icon name="spaces" size={23} />
        <strong>sweep</strong>
        <span>ATLAS</span>
      </span>
      <span>A world worth seeing. Together.</span>
      <div className="sh-header-actions">{controls ?? <button className="sh-atlas-fleet" onClick={onOpenFleet}>
        Fleet workspace <Icon name="arrow" size={14} />
      </button>}<AccountButton /></div>
    </header>
  )
}
